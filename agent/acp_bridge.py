"""ACP bridge — direct HTTP streaming connection to Hermes Agent gateway.

Replaces the docker-exec-based openclaw_bridge with a direct HTTP call to
the Hermes gateway's OpenAI-compatible API server endpoint.  This avoids
spawning a subprocess per message and enables streaming responses for
progressive TTS.

Prerequisites:
  - Each Hermes container must have API_SERVER_ENABLED=true in its .env
  - The API server listens on port 8642 inside the container (default)
  - Ports are mapped to the host: skynet-laira→127.0.0.1:3141,
    skynet-loki→127.0.0.1:3143 (configured via ACP_*_URL env vars)

The bridge exposes two main functions:
  send_to_gateway()       — non-streaming, returns full response (drop-in
                            replacement for send_to_openclaw)
  stream_from_gateway()   — async generator yielding ACPResponseChunk as
                            SSE deltas arrive from the gateway
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import AsyncIterator

import aiohttp

from acp_protocol import (
    ACPMessage,
    ACPResponseChunk,
    ChunkType,
    MessageType,
)

logger = logging.getLogger("acp-bridge")

# ---------------------------------------------------------------------------
# Gateway endpoint map
# ---------------------------------------------------------------------------

try:
    from agent_registry import get_gateway_urls, supports_streaming, agent_names
    _GATEWAY_URLS: dict[str, str] = get_gateway_urls()
    _STREAMING_AGENTS: set[str] = {n for n in agent_names() if supports_streaming(n)}
except ImportError:
    # Fallback if registry not available
    _GATEWAY_URLS = {
        "laira": os.getenv("ACP_LAIRA_URL", "http://127.0.0.1:3133").rstrip("/"),
        "loki": os.getenv("ACP_LOKI_URL", "http://172.20.0.3:8642").rstrip("/"),
    }
    _STREAMING_AGENTS = {
        name.lower()
        for name in os.getenv("ACP_STREAMING_AGENTS", "laira").split(",")
        if name.strip()
    }

_API_KEY: str = os.getenv("ACP_API_KEY", "").strip()

# Reusable session (created lazily, one per process)
_session: aiohttp.ClientSession | None = None

# Observability (reuse openclaw_bridge's event endpoint if configured)
_EVENT_ENDPOINT = os.getenv(
    "OPENCLAW_EVENT_ENDPOINT",
    "http://127.0.0.1:3210/api/observability/events",
).strip()
_EVENT_SOURCE_APP = os.getenv(
    "OPENCLAW_EVENT_SOURCE_APP",
    "LiveKitACP",
).strip() or "LiveKitACP"


def _get_gateway_url(agent_name: str) -> str:
    key = agent_name.lower()
    url = _GATEWAY_URLS.get(key, "http://127.0.0.1:3133")
    if not url.startswith("https://") and "127.0.0.1" not in url and "localhost" not in url and "172." not in url:
        logger.warning("ACP URL for %s is non-HTTPS on a non-local address: %s", agent_name, url)
    return url


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit_per_host=10, ttl_dns_cache=300, keepalive_timeout=30)
        timeout = aiohttp.ClientTimeout(total=120, connect=10)
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _session


async def close_session() -> None:
    """Close the shared aiohttp session (call on shutdown)."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


def _build_headers(session_id: str) -> dict[str, str]:
    # Sanitize session_id for safe HTTP header use
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', session_id)[:128]
    headers = {
        "Content-Type": "application/json",
        "X-Hermes-Session-Id": safe_id,
        "X-Hermes-Platform": "agora",
    }
    if _API_KEY:
        headers["Authorization"] = f"Bearer {_API_KEY}"
    return headers


def _room_from_session_id(session_id: str | None) -> str | None:
    if session_id and session_id.startswith("livekit-") and len(session_id) > 8:
        return session_id[8:]
    return None


def _trim(value: str, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

_obs_fail_count = 0
_obs_fail_last_warn = 0.0


async def _emit_event(
    *,
    event_type: str,
    agent_name: str,
    session_id: str | None,
    payload: dict,
) -> None:
    if not _EVENT_ENDPOINT:
        return
    event = {
        "source_app": _EVENT_SOURCE_APP,
        "session_id": session_id or f"livekit-{agent_name.lower()}",
        "hook_event_type": event_type,
        "payload": {**payload, "agent_name": agent_name, "tool_name": "ACP"},
        "timestamp": int(time.time() * 1000),
    }
    try:
        session = await _get_session()
        async with session.post(
            _EVENT_ENDPOINT,
            json=event,
            timeout=aiohttp.ClientTimeout(total=2),
        ):
            pass
    except Exception as exc:
        global _obs_fail_count, _obs_fail_last_warn
        _obs_fail_count += 1
        now = time.monotonic()
        if _obs_fail_count >= 5 and now - _obs_fail_last_warn > 60:
            logger.warning("Observability events failing (%d failures): %s", _obs_fail_count, exc)
            _obs_fail_last_warn = now
            _obs_fail_count = 0
        else:
            logger.debug("Observability event post failed: %s", exc)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def check_gateway_health(agent_name: str) -> dict:
    """Check if the Hermes API server is reachable for an agent."""
    url = f"{_get_gateway_url(agent_name)}/health"
    try:
        session = await _get_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {"ok": True, "status": data, "agent_name": agent_name}
            return {
                "ok": False,
                "agent_name": agent_name,
                "reason": f"Health check returned {resp.status}",
            }
    except Exception as exc:
        return {
            "ok": False,
            "agent_name": agent_name,
            "reason": f"Health check failed: {exc}",
        }


# ---------------------------------------------------------------------------
# Streaming response generator
# ---------------------------------------------------------------------------

async def stream_from_gateway(
    agent_name: str,
    message: ACPMessage,
    *,
    system_prompt: str | None = None,
    timeout: int = 120,
) -> AsyncIterator[ACPResponseChunk]:
    """Stream response chunks from the Hermes gateway via SSE.

    Yields ACPResponseChunk objects as text deltas arrive.  The final
    chunk has type=ChunkType.DONE.
    """
    base_url = _get_gateway_url(agent_name)
    url = f"{base_url}/v1/chat/completions"
    headers = _build_headers(message.session_id)
    use_streaming = agent_name.lower() in _STREAMING_AGENTS

    chat_messages = message.to_chat_messages(system_prompt=system_prompt)
    body = {
        "model": "hermes-agent",
        "messages": chat_messages,
        "stream": use_streaming,
    }

    started = time.monotonic()
    prompt_preview = _trim(message.content, 220)

    await _emit_event(
        event_type="ACPCallStart",
        agent_name=agent_name,
        session_id=message.session_id,
        payload={
            "status": "start",
            "prompt_chars": len(message.content),
            "prompt_preview": prompt_preview,
            "room": _room_from_session_id(message.session_id),
        },
    )

    try:
        session = await _get_session()
        async with session.post(
            url,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error("ACP gateway returned %d: %s", resp.status, error_text[:200])
                await _emit_event(
                    event_type="ACPCallError",
                    agent_name=agent_name,
                    session_id=message.session_id,
                    payload={
                        "status": "error",
                        "error": f"HTTP {resp.status}: {_trim(error_text, 200)}",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                yield ACPResponseChunk(
                    type=ChunkType.ERROR,
                    content=f"Gateway error (HTTP {resp.status})",
                )
                return

            if use_streaming:
                # Parse SSE stream
                full_text_parts: list[str] = []
                async for line in resp.content:
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str or not line_str.startswith("data: "):
                        continue
                    data_str = line_str[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk_data.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        full_text_parts.append(content)
                        yield ACPResponseChunk(
                            type=ChunkType.TEXT_CHUNK,
                            content=content,
                        )

                full_text = "".join(full_text_parts)
            else:
                # Non-streaming: parse full JSON response
                resp_data = await resp.json()
                choices = resp_data.get("choices", [])
                full_text = ""
                if choices:
                    full_text = choices[0].get("message", {}).get("content", "")
                if full_text:
                    yield ACPResponseChunk(
                        type=ChunkType.TEXT_CHUNK,
                        content=full_text,
                    )
            duration_ms = round((time.monotonic() - started) * 1000)

            await _emit_event(
                event_type="ACPCallComplete",
                agent_name=agent_name,
                session_id=message.session_id,
                payload={
                    "status": "success",
                    "duration_ms": duration_ms,
                    "prompt_chars": len(message.content),
                    "prompt_preview": prompt_preview,
                    "response_chars": len(full_text),
                    "response_preview": _trim(full_text, 240),
                    "room": _room_from_session_id(message.session_id),
                },
            )

            yield ACPResponseChunk(type=ChunkType.DONE, content=full_text)

    except asyncio.TimeoutError:
        logger.error("ACP gateway timed out after %ds", timeout)
        await _emit_event(
            event_type="ACPCallError",
            agent_name=agent_name,
            session_id=message.session_id,
            payload={
                "status": "timeout",
                "error": f"ACP timed out after {timeout}s",
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        yield ACPResponseChunk(
            type=ChunkType.ERROR,
            content="ACP gateway timed out",
        )

    except aiohttp.ClientError as exc:
        logger.error("ACP connection error: %s", exc)
        await _emit_event(
            event_type="ACPCallError",
            agent_name=agent_name,
            session_id=message.session_id,
            payload={
                "status": "error",
                "error": _trim(str(exc), 260),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        yield ACPResponseChunk(
            type=ChunkType.ERROR,
            content=f"ACP connection error: {exc}",
        )

    except Exception as exc:
        logger.error("ACP bridge error: %s", exc)
        await _emit_event(
            event_type="ACPCallError",
            agent_name=agent_name,
            session_id=message.session_id,
            payload={
                "status": "error",
                "error": _trim(str(exc), 260),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        yield ACPResponseChunk(
            type=ChunkType.ERROR,
            content=f"Bridge error: {exc}",
        )


# ---------------------------------------------------------------------------
# Non-streaming wrapper (drop-in replacement for send_to_openclaw)
# ---------------------------------------------------------------------------

async def send_to_gateway(
    agent_name: str,
    message: str,
    *,
    session_id: str | None = None,
    system_prompt: str | None = None,
    sender: str = "user",
    timeout: int = 120,
) -> dict:
    """Send a message and collect the full response (non-streaming).

    Returns dict with keys: ok (bool), text (str), raw (dict).
    Compatible with the openclaw_bridge.send_to_openclaw return format.
    Retries once on connection errors.
    """
    msg = ACPMessage(
        type=MessageType.VOICE_INPUT,
        session_id=session_id or f"livekit-{agent_name.lower()}",
        sender=sender,
        content=message,
        metadata={"modality": "voice"},
    )

    for _attempt in range(2):
        text_parts: list[str] = []
        error: str | None = None

        async for chunk in stream_from_gateway(
            agent_name, msg, system_prompt=system_prompt, timeout=timeout,
        ):
            if chunk.type == ChunkType.TEXT_CHUNK:
                text_parts.append(chunk.content)
            elif chunk.type == ChunkType.ERROR:
                error = chunk.content
            elif chunk.type == ChunkType.DONE:
                pass

        if error and not text_parts and _attempt == 0:
            logger.warning("ACP call failed (%s), retrying...", error)
            await asyncio.sleep(0.5)
            continue

        if error and not text_parts:
            return {"ok": False, "text": error, "raw": {}}

        full_text = "".join(text_parts)
        if not full_text.strip():
            return {"ok": False, "text": "Gateway returned empty response", "raw": {}}

        return {"ok": True, "text": full_text.strip(), "raw": {"source": "acp"}}

    return {"ok": False, "text": "ACP call failed after retry", "raw": {}}
