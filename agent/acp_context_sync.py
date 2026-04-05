"""Cross-session context sync for Agora (legacy JSONL approach — superseded by ACP Event Bus).

Publishes room events (voice input, agent responses, agent-to-agent messages)
to a shared JSONL log so other sessions (Telegram, Discord, etc.) can read
the latest voice-room activity.

The log file lives at CONTEXT_SYNC_PATH (default /tmp/virtualcomms-context.jsonl)
and contains one JSON object per line:

    {"ts": 1712345678.123, "room": "skynet-comms", "speaker": "Giannis",
     "agent": "laira", "type": "voice_input", "content": "hello everyone"}

A maximum of CONTEXT_SYNC_MAX_LINES recent lines are kept (older entries
are pruned on write to prevent unbounded growth).
"""

from __future__ import annotations

# TODO: Wire read_recent() into Hermes/OpenClaw system prompts for cross-session awareness.
# Currently write-only — Telegram/Discord sessions need an adapter to consume these events.

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("acp-context-sync")

CONTEXT_SYNC_PATH = Path(
    os.getenv("CONTEXT_SYNC_PATH", "/tmp/virtualcomms-context.jsonl")
)
CONTEXT_SYNC_MAX_LINES = int(os.getenv("CONTEXT_SYNC_MAX_LINES", "200"))

_write_lock = threading.Lock()
_line_count = 0
_line_count_loaded = False


def publish_event(
    *,
    room: str,
    speaker: str,
    agent: str,
    event_type: str,
    content: str,
) -> None:
    """Append a context event to the shared log (non-blocking, best-effort)."""
    if not content or not content.strip():
        return

    entry = {
        "ts": time.time(),
        "room": room,
        "speaker": speaker,
        "agent": agent.lower(),
        "type": event_type,
        "content": content.strip()[:500],
    }

    threading.Thread(target=_do_write, args=(entry,), daemon=True).start()


def _do_write(entry: dict) -> None:
    global _line_count, _line_count_loaded
    try:
        with _write_lock:
            if not _line_count_loaded:
                try:
                    _line_count = len(CONTEXT_SYNC_PATH.read_text(encoding="utf-8").splitlines())
                except FileNotFoundError:
                    _line_count = 0
                _line_count_loaded = True

            with open(CONTEXT_SYNC_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _line_count += 1

            if _line_count > CONTEXT_SYNC_MAX_LINES * 1.5:
                lines = CONTEXT_SYNC_PATH.read_text(encoding="utf-8").splitlines()
                keep = lines[-CONTEXT_SYNC_MAX_LINES:]
                CONTEXT_SYNC_PATH.write_text("\n".join(keep) + "\n", encoding="utf-8")
                _line_count = len(keep)
    except Exception as exc:
        pass  # best-effort


def read_recent(n: int = 20, room: str | None = None) -> list[dict]:
    """Read the last *n* context events, optionally filtered by room."""
    try:
        lines = CONTEXT_SYNC_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []

    events = []
    for line in lines[-n * 2:]:  # read extra to allow for room filter
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if room and entry.get("room") != room:
                continue
            events.append(entry)
        except json.JSONDecodeError:
            continue

    return events[-n:]
