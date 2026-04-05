#!/usr/bin/env python3
"""Minimal OpenAI-compatible HTTP API shim for OpenClaw gateway.

Deploys inside an OpenClaw container to accept /v1/chat/completions
requests over HTTP and proxy them through `openclaw agent`.  This
eliminates docker-exec overhead and gives the ACP bridge a standard
HTTP endpoint identical to Hermes' API server.

Supports SSE streaming (stream=true) by reading openclaw agent stdout
line-by-line and emitting SSE chunks as text arrives.

Listens on 0.0.0.0:8642 by default (overridable via SHIM_PORT env var).
Supports X-Hermes-Session-Id header for session continuity.

Usage (inside the container):
    python3 /home/node/openclaw_api_shim.py &
"""

# DEPLOYMENT: Add to container entrypoint for persistence across restarts:
#   su -s /bin/bash node -c "python3 /home/node/openclaw_api_shim.py" &

import asyncio
import json
import os
import re
import sys
import time
import uuid
from http import HTTPStatus

SHIM_PORT = int(os.environ.get("SHIM_PORT", "8642"))
SHIM_HOST = os.environ.get("SHIM_HOST", "0.0.0.0")
SHIM_API_KEY = os.environ.get("SHIM_API_KEY", "").strip()
MAX_BODY_SIZE = 1_000_000  # 1 MB

# Regex to strip ANSI escape codes from openclaw output
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07')


async def run_openclaw_agent(message: str, session_id: str, timeout: int = 60) -> dict:
    """Run `openclaw agent` and return parsed JSON result."""
    cmd = [
        "openclaw", "agent",
        "--message", message,
        "--json",
        "--thinking", "off",
        "--session-id", session_id,
        "--timeout", str(timeout),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 10)
    if proc.returncode != 0:
        print(f"[openclaw-api-shim] agent failed (rc={proc.returncode})", file=sys.stderr, flush=True)
        return {"error": "Agent request failed"}
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return {"error": "Invalid JSON from openclaw agent", "raw": stdout.decode()[:500]}


# Sentence boundary: .!? followed by whitespace and a letter (avoids splitting abbreviations).
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Za-z])')


async def run_openclaw_agent_streaming(
    message: str, session_id: str, timeout: int = 60
):
    """Run `openclaw agent --json`, then yield the response text split by sentence.

    OpenClaw CLI outputs the full response at once (not progressively), so we
    run with --json to get the structured result, then split the text into
    sentences and yield them as pseudo-streaming chunks.  This lets the ACP
    bridge feed sentences to the TTS queue one at a time.
    """
    result = await run_openclaw_agent(message, session_id, timeout=timeout)
    if "error" in result:
        return

    payloads = result.get("result", {}).get("payloads", [])
    text = "\n".join(p["text"] for p in payloads if p.get("text"))
    if not text:
        return

    # Split into sentences and yield each one
    sentences = _SENTENCE_SPLIT.split(text)
    for sentence in sentences:
        sentence = sentence.strip()
        if sentence:
            yield sentence


class HTTPRequest:
    """Minimal HTTP request parser for asyncio streams."""

    def __init__(self, method: str, path: str, headers: dict, body: bytes):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    @classmethod
    async def read_from(cls, reader: asyncio.StreamReader) -> "HTTPRequest":
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        parts = line.decode().strip().split(" ", 2)
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        headers: dict[str, str] = {}
        while True:
            hline = await asyncio.wait_for(reader.readline(), timeout=10)
            hline_str = hline.decode().strip()
            if not hline_str:
                break
            if ":" in hline_str:
                k, v = hline_str.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        body = b""
        cl = headers.get("content-length")
        if cl and cl.isdigit():
            cl_int = int(cl)
            if cl_int > MAX_BODY_SIZE:
                raise ValueError(f"Content-Length {cl_int} exceeds {MAX_BODY_SIZE}")
            body = await asyncio.wait_for(reader.readexactly(cl_int), timeout=30)

        return cls(method, path, headers, body)


def _write_response(writer: asyncio.StreamWriter, status: int, body: dict, extra_headers: dict | None = None):
    """Write an HTTP JSON response."""
    payload = json.dumps(body).encode()
    status_text = HTTPStatus(status).phrase
    lines = [
        f"HTTP/1.1 {status} {status_text}",
        "Content-Type: application/json",
        f"Content-Length: {len(payload)}",
        "Connection: close",
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("")
    writer.write("\r\n".join(lines).encode() + payload)


def _write_sse_headers(writer: asyncio.StreamWriter, session_id: str):
    """Write SSE response headers."""
    lines = [
        "HTTP/1.1 200 OK",
        "Content-Type: text/event-stream",
        "Cache-Control: no-cache",
        "Connection: keep-alive",
        f"X-Hermes-Session-Id: {session_id}",
        "",
        "",
    ]
    writer.write("\r\n".join(lines).encode())


def _make_sse_chunk(text: str, chunk_id: str, model: str = "openclaw-agent") -> str:
    """Format a single SSE data line in OpenAI chat.completion.chunk format."""
    data = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": text},
            "finish_reason": None,
        }],
    }
    return f"data: {json.dumps(data)}\n\n"


def _make_sse_done() -> str:
    return "data: [DONE]\n\n"


def _check_auth(req: HTTPRequest) -> bool:
    if not SHIM_API_KEY:
        return True
    auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip() == SHIM_API_KEY
    return False


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        req = await HTTPRequest.read_from(reader)
    except ValueError as ve:
        _write_response(writer, 413, {"error": {"message": str(ve)}})
        await writer.drain()
        writer.close()
        return
    except Exception:
        writer.close()
        return

    try:
        if req.method == "GET" and req.path == "/health":
            _write_response(writer, 200, {"status": "ok", "platform": "openclaw-shim", "streaming": True})

        elif not _check_auth(req):
            _write_response(writer, 401, {"error": {"message": "Unauthorized", "type": "invalid_api_key"}})

        elif req.method == "GET" and req.path == "/v1/models":
            _write_response(writer, 200, {
                "object": "list",
                "data": [{"id": "openclaw-agent", "object": "model", "owned_by": "openclaw"}],
            })

        elif req.method == "POST" and req.path == "/v1/chat/completions":
            try:
                body = json.loads(req.body)
            except json.JSONDecodeError:
                _write_response(writer, 400, {"error": {"message": "Invalid JSON"}})
                await writer.drain()
                writer.close()
                return

            messages = body.get("messages", [])
            user_message = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    user_message = m.get("content", "")
                    break

            if not user_message:
                _write_response(writer, 400, {"error": {"message": "No user message"}})
                await writer.drain()
                writer.close()
                return

            session_id = req.headers.get("x-hermes-session-id", str(uuid.uuid4()))
            timeout_val = int(body.get("timeout", 60))
            use_streaming = body.get("stream", False)

            if use_streaming:
                # SSE streaming response
                chunk_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
                _write_sse_headers(writer, session_id)
                await writer.drain()

                full_text_parts = []
                try:
                    async for text_chunk in run_openclaw_agent_streaming(
                        user_message, session_id, timeout=timeout_val
                    ):
                        full_text_parts.append(text_chunk)
                        sse_data = _make_sse_chunk(text_chunk + " ", chunk_id)
                        writer.write(sse_data.encode())
                        await writer.drain()

                    writer.write(_make_sse_done().encode())
                    await writer.drain()
                except Exception as e:
                    print(f"[openclaw-api-shim] streaming error: {e}", file=sys.stderr, flush=True)
                    # If we already started streaming, just close
                    if full_text_parts:
                        writer.write(_make_sse_done().encode())
                        await writer.drain()
            else:
                # Non-streaming response
                result = await run_openclaw_agent(user_message, session_id, timeout=timeout_val)

                if "error" in result:
                    _write_response(writer, 500, {
                        "error": {"message": "Internal server error", "type": "server_error"},
                    }, {"X-Hermes-Session-Id": session_id})
                else:
                    payloads = result.get("result", {}).get("payloads", [])
                    text = "\n".join(p["text"] for p in payloads if p.get("text"))
                    model = result.get("result", {}).get("meta", {}).get("agentMeta", {}).get("model", "openclaw-agent")

                    _write_response(writer, 200, {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
                    }, {"X-Hermes-Session-Id": session_id})

        else:
            _write_response(writer, 404, {"error": {"message": "Not found"}})

        await writer.drain()
    except Exception as e:
        try:
            print(f"[openclaw-api-shim] handler error: {e}", file=sys.stderr, flush=True)
            _write_response(writer, 500, {"error": {"message": "Internal server error"}})
            await writer.drain()
        except Exception:
            pass
    finally:
        writer.close()


async def main():
    server = await asyncio.start_server(handle_client, SHIM_HOST, SHIM_PORT)
    if not SHIM_API_KEY:
        print("[openclaw-api-shim] WARNING: Running WITHOUT API key. Set SHIM_API_KEY for production.", file=sys.stderr, flush=True)
    print(f"[openclaw-api-shim] Listening on {SHIM_HOST}:{SHIM_PORT} (streaming=true, auth={'yes' if SHIM_API_KEY else 'NO'})", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
