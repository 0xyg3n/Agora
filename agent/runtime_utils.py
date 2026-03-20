"""Pure helpers for bounded context and fallback behavior.

These helpers are intentionally stdlib-only so they can be regression-tested
without starting LiveKit, OpenClaw, or the agent runtime.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


def normalize_context_text(text: str, max_entry_chars: int) -> str:
    """Collapse whitespace and cap a context line."""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if len(normalized) > max_entry_chars:
        if max_entry_chars > 3:
            normalized = normalized[: max_entry_chars - 3].rstrip() + "..."
        else:
            normalized = normalized[:max_entry_chars]
    return normalized


def mentions_name(text: str, name: str) -> bool:
    """Return whether `name` appears as a whole-word mention in `text`."""
    cleaned_text = (text or "").strip().lower()
    cleaned_name = (name or "").strip().lower()
    if not cleaned_text or not cleaned_name:
        return False
    return re.search(rf"\b{re.escape(cleaned_name)}\b", cleaned_text) is not None


def is_directly_addressed(text: str, name: str) -> bool:
    """Return whether `text` clearly addresses `name`, not just mentions it."""
    cleaned_text = re.sub(r"\s+", " ", (text or "").strip().lower())
    cleaned_name = (name or "").strip().lower()
    if not cleaned_text or not cleaned_name:
        return False

    if cleaned_text.startswith(f"{cleaned_name},") or cleaned_text.startswith(f"{cleaned_name}:"):
        return True
    if re.search(rf"\b(?:hey|hi|yo|ok|okay)\s+{re.escape(cleaned_name)}\b", cleaned_text):
        return True
    if re.match(rf"^{re.escape(cleaned_name)}\b", cleaned_text):
        return True
    if re.search(rf"[,.;:!?]\s*{re.escape(cleaned_name)}[,:!?]?\s*$", cleaned_text):
        return True
    return False


def classify_agent_turn_trigger(text: str, our_name: str) -> str | None:
    """Classify whether another agent message should trigger our reply."""
    if is_directly_addressed(text, our_name):
        return "direct"
    if mentions_name(text, our_name):
        return "mention"
    return None


def should_store_context_message(
    text: str,
    *,
    low_value_lines: set[str],
    max_entry_chars: int,
) -> bool:
    """Return whether a context line is worth persisting and replaying."""
    normalized = normalize_context_text(text, max_entry_chars)
    if not normalized:
        return False
    return normalized.lower() not in low_value_lines


def build_room_context(
    entries: Sequence[tuple[str, str, float]],
    *,
    max_messages: int,
    max_chars: int,
    max_entry_chars: int,
) -> str:
    """Build the bounded recent room-context block for OpenClaw."""
    if not entries or max_messages <= 0 or max_chars <= 0:
        return ""

    parts: list[str] = []
    total_chars = 0
    recent = entries[-max_messages:]

    for sender, text, _ in reversed(recent):
        normalized = normalize_context_text(text, max_entry_chars)
        if not normalized:
            continue

        entry = f"[{sender}]: {normalized}"
        entry_len = len(entry) + 1

        if parts and total_chars + entry_len > max_chars:
            break

        if not parts and entry_len > max_chars:
            entry = entry[:max_chars].rstrip()
            entry_len = len(entry)

        parts.append(entry)
        total_chars += entry_len

    if not parts:
        return ""

    parts.reverse()
    return "[Recent room context]:\n" + "\n".join(parts) + "\n\n"


def classify_openclaw_result(
    result: Mapping[str, Any],
    *,
    openclaw_fallback: str,
    timeout_fallback: str,
    empty_reply_fallback: str,
) -> dict[str, str | bool]:
    """Classify an OpenClaw bridge result into success or safe spoken fallback."""
    result_text = str(result.get("text") or "").strip()

    if not result.get("ok"):
        lower = result_text.lower()
        if "timed out" in lower:
            return {
                "ok": False,
                "spoken_text": timeout_fallback,
                "status": "OpenClaw timed out",
                "safe_error": "OpenClaw timed out",
                "result_text": result_text,
            }
        if "bridge error" in lower:
            return {
                "ok": False,
                "spoken_text": openclaw_fallback,
                "status": "Bridge request failed",
                "safe_error": "Bridge failure",
                "result_text": result_text,
            }
        return {
            "ok": False,
            "spoken_text": openclaw_fallback,
            "status": "OpenClaw request failed",
            "safe_error": "OpenClaw failure",
            "result_text": result_text,
        }

    if not result_text:
        return {
            "ok": False,
            "spoken_text": empty_reply_fallback,
            "status": "OpenClaw reply was empty",
            "safe_error": "OpenClaw returned empty reply",
            "result_text": result_text,
        }

    return {
        "ok": True,
        "spoken_text": result_text,
        "status": "Reply ready",
        "safe_error": "",
        "result_text": result_text,
    }


def ensure_spoken_response_text(response: str | None, empty_reply_fallback: str) -> tuple[str, bool]:
    """Guarantee a non-empty spoken reply for TTS."""
    cleaned = (response or "").strip()
    if cleaned:
        return cleaned, False
    return empty_reply_fallback, True


def is_vision_failure_text(text: str | None, failure_prefixes: tuple[str, ...]) -> bool:
    """Detect helper-generated vision failure replies that should stay in error state."""
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return False
    return cleaned.startswith(failure_prefixes)
