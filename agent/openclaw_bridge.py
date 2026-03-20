"""Bridge between LiveKit voice agents and OpenClaw containers.

Allows voice agents to:
- Delegate actions to their OpenClaw instance (send messages, search memory, etc.)
- Read workspace files live from the container
"""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

logger = logging.getLogger("openclaw-bridge")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OPENCLAW_VERSION_FILE = _REPO_ROOT / "config" / "openclaw-version.txt"
_OPENCLAW_VERSION_PATTERN = re.compile(r"OpenClaw\s+([0-9]+(?:\.[0-9]+)+)")
_OPENCLAW_CONFIG_PATH = "/home/node/.openclaw/openclaw.json"
_OPENCLAW_COMPAT_CACHE_TTL = max(0, int(os.getenv("OPENCLAW_COMPAT_CACHE_TTL", "300")))
_OPENCLAW_EVENT_ENDPOINT = os.getenv(
    "OPENCLAW_EVENT_ENDPOINT",
    "http://127.0.0.1:3210/api/observability/events",
).strip()
_OPENCLAW_EVENT_SOURCE_APP = os.getenv(
    "OPENCLAW_EVENT_SOURCE_APP",
    "LiveKitOpenClaw",
).strip() or "LiveKitOpenClaw"
_compatibility_cache: dict[str, tuple[float, dict]] = {}

# Map agent names to their OpenClaw container names
_CONTAINER_MAP = {
    "laira": "skynet-laira",
    "loki": "skynet-loki",
}


def _get_container(agent_name: str) -> str:
    return _CONTAINER_MAP.get(agent_name.lower(), f"skynet-{agent_name.lower()}")


def _get_required_openclaw_version() -> str | None:
    override = os.getenv("OPENCLAW_REQUIRED_VERSION", "").strip()
    if override:
        return override
    try:
        pinned = _OPENCLAW_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pinned or None


def _extract_openclaw_version(output: str) -> str | None:
    match = _OPENCLAW_VERSION_PATTERN.search(output)
    return match.group(1) if match else None


def _parse_version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not version:
        return None
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def _evaluate_openclaw_runtime(
    *,
    required_version: str | None,
    installed_version: str | None,
    config_version: str | None,
) -> dict:
    compatible = True
    reason = "OpenClaw runtime is compatible"

    if not installed_version:
        compatible = False
        reason = "Unable to detect installed OpenClaw version"
    elif required_version and installed_version != required_version:
        compatible = False
        reason = (
            "OpenClaw version mismatch for LiveKit: "
            f"required {required_version}, found {installed_version}"
        )
    else:
        installed_parts = _parse_version_tuple(installed_version)
        config_parts = _parse_version_tuple(config_version)
        if config_version and installed_parts and config_parts and installed_parts < config_parts:
            compatible = False
            reason = (
                "OpenClaw config was written by a newer version: "
                f"config {config_version}, installed {installed_version}"
            )

    return {
        "required_version": required_version,
        "installed_version": installed_version,
        "config_version": config_version,
        "compatible": compatible,
        "reason": reason,
    }


async def _exec_container(
    container: str,
    *args: str,
    user: str | None = None,
    timeout: int = 10,
) -> tuple[int, str, str]:
    cmd = ["docker", "exec"]
    if user:
        cmd.extend(["--user", user])
    cmd.extend([container, *args])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace").strip(),
    )


async def get_openclaw_runtime_status(
    agent_name: str,
    *,
    refresh: bool = False,
) -> dict:
    container = _get_container(agent_name)
    cached = _compatibility_cache.get(container)
    now = time.monotonic()
    if (
        not refresh
        and cached is not None
        and _OPENCLAW_COMPAT_CACHE_TTL > 0
        and (now - cached[0]) < _OPENCLAW_COMPAT_CACHE_TTL
    ):
        return dict(cached[1])

    required_version = _get_required_openclaw_version()
    base_status = {
        "agent_name": agent_name,
        "container": container,
        "required_version": required_version,
        "installed_version": None,
        "config_version": None,
        "compatible": False,
        "reason": "",
    }

    try:
        code, stdout, stderr = await _exec_container(
            container,
            "openclaw",
            "--version",
            timeout=10,
        )
    except Exception as exc:
        status = {
            **base_status,
            "reason": f"Unable to inspect OpenClaw runtime: {exc}",
        }
    else:
        if code != 0:
            status = {
                **base_status,
                "reason": (
                    "Unable to inspect OpenClaw runtime"
                    + (f": {stderr}" if stderr else "")
                ),
            }
        else:
            installed_version = _extract_openclaw_version(stdout)
            config_version: str | None = None
            try:
                config_code, config_stdout, _ = await _exec_container(
                    container,
                    "cat",
                    _OPENCLAW_CONFIG_PATH,
                    user="node",
                    timeout=5,
                )
                if config_code == 0:
                    config_raw = json.loads(config_stdout)
                    config_version = (
                        config_raw.get("meta", {}).get("lastTouchedVersion") or None
                    )
            except Exception:
                config_version = None

            status = {
                "agent_name": agent_name,
                "container": container,
                **_evaluate_openclaw_runtime(
                    required_version=required_version,
                    installed_version=installed_version,
                    config_version=config_version,
                ),
            }

    _compatibility_cache[container] = (now, status)
    return dict(status)


def _build_openclaw_agent_cmd(
    *,
    container: str,
    message: str,
    agent_id: str | None,
    session_id: str | None,
    timeout: int,
) -> list[str]:
    """Build the OpenClaw CLI command for a single agent turn.

    LiveKit isolation must come from `session_id`, not from inventing a fake
    OpenClaw agent id like "livekit".
    """
    cmd = [
        "docker", "exec", "--user", "node", container,
        "openclaw", "agent",
    ]
    if agent_id:
        cmd.extend(["--agent", agent_id])
    if session_id:
        cmd.extend(["--session-id", session_id])
    cmd.extend([
        "--message", message,
        "--json",
        "--thinking", "off",
        "--timeout", str(timeout),
    ])
    return cmd


def _room_from_session_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    if session_id.startswith("livekit-") and len(session_id) > len("livekit-"):
        return session_id[len("livekit-"):]
    return None


def _trim_event_text(value: str, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _post_observability_event(event: dict) -> None:
    if not _OPENCLAW_EVENT_ENDPOINT:
        return

    req = urllib.request.Request(
        _OPENCLAW_EVENT_ENDPOINT,
        data=json.dumps(event).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "LiveKit-OpenClaw-Bridge/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=1.5) as response:
        response.read()
        if response.status >= 400:
            raise RuntimeError(f"Observability endpoint returned {response.status}")


async def _emit_observability_event(
    *,
    event_type: str,
    agent_name: str,
    session_id: str | None,
    payload: dict,
    model_name: str | None = None,
) -> None:
    if not _OPENCLAW_EVENT_ENDPOINT:
        return

    event = {
        "source_app": _OPENCLAW_EVENT_SOURCE_APP,
        "session_id": session_id or f"livekit-{agent_name.lower()}",
        "hook_event_type": event_type,
        "payload": {
            **payload,
            "agent_name": agent_name,
            "room": _room_from_session_id(session_id),
            "tool_name": "OpenClaw",
        },
        "timestamp": int(time.time() * 1000),
    }
    if model_name:
        event["model_name"] = model_name

    try:
        await asyncio.to_thread(_post_observability_event, event)
    except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
        logger.debug("Observability event post failed: %s", exc)


async def send_to_openclaw(
    agent_name: str,
    message: str,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    timeout: int = 120,
) -> dict:
    """Send a message to the OpenClaw agent and return the response.

    Returns dict with keys: ok (bool), text (str), raw (dict).
    """
    compatibility = await get_openclaw_runtime_status(agent_name)
    container = _get_container(agent_name)
    room = _room_from_session_id(session_id)
    prompt_preview = _trim_event_text(message, 220)
    started_at = time.monotonic()

    if not compatibility["compatible"]:
        logger.error(
            "OpenClaw compatibility check failed for %s: %s",
            agent_name,
            compatibility["reason"],
        )
        await _emit_observability_event(
            event_type="OpenClawCompatibilityError",
            agent_name=agent_name,
            session_id=session_id,
            payload={
                "status": "error",
                "error": compatibility["reason"],
                "container": container,
                "prompt_chars": len(message),
                "prompt_preview": prompt_preview,
                "room": room,
            },
        )
        return {
            "ok": False,
            "text": compatibility["reason"],
            "raw": {"compatibility": compatibility},
        }

    cmd = _build_openclaw_agent_cmd(
        container=container,
        message=message,
        agent_id=agent_id,
        session_id=session_id,
        timeout=timeout,
    )

    await _emit_observability_event(
        event_type="OpenClawCallStart",
        agent_name=agent_name,
        session_id=session_id,
        payload={
            "status": "start",
            "container": container,
            "prompt_chars": len(message),
            "prompt_preview": prompt_preview,
            "agent_id": agent_id or "",
            "room": room,
        },
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout + 10
        )

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.error(f"OpenClaw command failed: {err}")
            await _emit_observability_event(
                event_type="OpenClawCallError",
                agent_name=agent_name,
                session_id=session_id,
                payload={
                    "status": "error",
                    "error": _trim_event_text(err, 260) or "OpenClaw command failed",
                    "container": container,
                    "duration_ms": round((time.monotonic() - started_at) * 1000),
                    "prompt_chars": len(message),
                    "prompt_preview": prompt_preview,
                    "room": room,
                },
            )
            return {"ok": False, "text": f"OpenClaw error: {err}", "raw": {}}

        raw = json.loads(stdout.decode())
        # Extract text from response payloads
        payloads = raw.get("result", {}).get("payloads", [])
        text_parts = [p["text"] for p in payloads if p.get("text")]
        text = "\n".join(text_parts)

        await _emit_observability_event(
            event_type="OpenClawCallComplete",
            agent_name=agent_name,
            session_id=session_id,
            payload={
                "status": "success",
                "container": container,
                "duration_ms": round((time.monotonic() - started_at) * 1000),
                "prompt_chars": len(message),
                "prompt_preview": prompt_preview,
                "response_chars": len(text),
                "response_preview": _trim_event_text(text, 240),
                "room": room,
            },
            model_name=raw.get("result", {}).get("model")
            or raw.get("model")
            or None,
        )

        return {"ok": True, "text": text, "raw": raw}

    except asyncio.TimeoutError:
        logger.error(f"OpenClaw command timed out after {timeout}s")
        await _emit_observability_event(
            event_type="OpenClawCallError",
            agent_name=agent_name,
            session_id=session_id,
            payload={
                "status": "timeout",
                "error": f"OpenClaw timed out after {timeout}s",
                "container": container,
                "duration_ms": round((time.monotonic() - started_at) * 1000),
                "prompt_chars": len(message),
                "prompt_preview": prompt_preview,
                "room": room,
            },
        )
        return {"ok": False, "text": "OpenClaw timed out", "raw": {}}
    except Exception as e:
        logger.error(f"OpenClaw bridge error: {e}")
        await _emit_observability_event(
            event_type="OpenClawCallError",
            agent_name=agent_name,
            session_id=session_id,
            payload={
                "status": "error",
                "error": _trim_event_text(str(e), 260),
                "container": container,
                "duration_ms": round((time.monotonic() - started_at) * 1000),
                "prompt_chars": len(message),
                "prompt_preview": prompt_preview,
                "room": room,
            },
        )
        return {"ok": False, "text": f"Bridge error: {e}", "raw": {}}


async def read_workspace_file(agent_name: str, filename: str) -> str | None:
    """Read a file from the OpenClaw workspace live."""
    container = _get_container(agent_name)
    path = f"/home/node/.openclaw/workspace/{filename}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "--user", "node", container, "cat", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None
        return stdout.decode(errors="replace")
    except Exception:
        return None


async def search_memory(agent_name: str, query: str) -> str | None:
    """Search the agent's OpenClaw memory for relevant context."""
    result = await send_to_openclaw(
        agent_name,
        f"Search your memory for: {query}. Reply with just the relevant facts, no commentary.",
        timeout=30,
    )
    return result["text"] if result["ok"] else None


async def _run_cli_check(agent_names: list[str]) -> int:
    names = agent_names or [name.title() for name in _CONTAINER_MAP]
    exit_code = 0
    for agent_name in names:
        status = await get_openclaw_runtime_status(agent_name, refresh=True)
        detail = (
            f"installed={status['installed_version'] or 'unknown'} "
            f"required={status['required_version'] or 'unset'} "
            f"config={status['config_version'] or 'unknown'}"
        )
        if status["compatible"]:
            print(f"[OK] {agent_name}: {detail}")
        else:
            print(f"[FAIL] {agent_name}: {status['reason']} ({detail})")
            exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LiveKit/OpenClaw compatibility checks")
    parser.add_argument(
        "--check",
        nargs="*",
        metavar="AGENT",
        help="Check pinned OpenClaw compatibility for one or more agents",
    )
    args = parser.parse_args(argv)

    if args.check is not None:
        return asyncio.run(_run_cli_check(args.check))

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
