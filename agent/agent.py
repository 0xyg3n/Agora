"""Voice agent for Skynet Comms — thin voice I/O layer over OpenClaw.

Architecture:
  Human speaks (mic) → VAD → STT → user_input_transcribed → OpenClaw → session.say() → TTS
  Human types (chat) → text_input_cb → OpenClaw → session.say() → TTS
  Agent-to-agent: text chat on lk.agent.chat (context sharing + mention-triggered replies)

No LiveKit LLM pipeline is used. All intelligence comes from OpenClaw sessions.
Voice transcriptions are echoed to lk.chat so they appear in the UI with the human's name.
"""

import asyncio
import json
import logging
import os
import re
import time

from dotenv import load_dotenv
from livekit import rtc

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RoomInputOptions,
    cli,
)
from livekit.plugins import silero

from edge_tts_plugin import EdgeTTS
from whisper_stt_plugin import WhisperSTT
from openclaw_bridge import send_to_openclaw
from openclaw_llm_plugin import NoOpLLM
from runtime_utils import (
    build_room_context,
    classify_agent_turn_trigger,
    classify_openclaw_result,
    ensure_spoken_response_text,
    is_directly_addressed,
    is_vision_failure_text,
    mentions_name,
    normalize_context_text,
    should_store_context_message,
)
from vision import is_vision_request, capture_frame, ask_claude_vision

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

logger = logging.getLogger("skynet-agent")

agent_name = os.environ.get("AGENT_NAME", "Laira")

# --- Singletons: loaded once per process, reused across room joins ---
_vad_instance = None
_stt_instance = None
_noop_llm = NoOpLLM()


def _get_vad():
    global _vad_instance
    if _vad_instance is None:
        logger.info("Loading Silero VAD model (once per process)")
        _vad_instance = silero.VAD.load()
    return _vad_instance


def _get_stt():
    global _stt_instance
    if _stt_instance is None:
        whisper_model = os.environ.get("WHISPER_MODEL", "small")
        logger.info(f"Creating WhisperSTT instance (once per process, model={whisper_model})")
        _stt_instance = WhisperSTT(model_size=whisper_model)
    return _stt_instance

# Known agents for turn-taking
_AGENT_NAMES = {"laira", "loki"}

# Common STT misrecognitions → correct name
_NAME_CORRECTIONS = {
    "lucky": "loki", "lockey": "loki", "locky": "loki",
    "laky": "loki", "loca": "loki", "lokay": "loki",
    "laura": "laira", "lyra": "laira", "lara": "laira",
    "lair": "laira", "layer": "laira", "layra": "laira",
}


def _correct_names(text: str) -> str:
    """Fix common STT misrecognitions of agent names."""
    words = text.split()
    corrected = []
    for word in words:
        clean = word.lower().rstrip(",.!?:;")
        if clean in _NAME_CORRECTIONS:
            # Preserve punctuation
            suffix = word[len(clean):]
            corrected.append(_NAME_CORRECTIONS[clean] + suffix)
        else:
            corrected.append(word)
    return " ".join(corrected)

# Static greetings — zero LLM calls
_GREETINGS = {
    "laira": "Hey, Laira here!",
    "loki": "Yo, Loki in the house.",
}

server = AgentServer()


@server.rtc_session(agent_name=agent_name)
async def entrypoint(ctx: JobContext):
    voice = os.environ.get("EDGE_TTS_VOICE", "en-US-AriaNeural")
    room_name = ctx.room.name

    logger.info(f"Starting agent '{agent_name}' in room '{room_name}'")

    session = AgentSession(
        vad=_get_vad(),
        stt=_get_stt(),
        llm=_noop_llm,
        tts=EdgeTTS(voice=voice),
        allow_interruptions=True,
        min_endpointing_delay=1.2,
        max_endpointing_delay=5.0,
    )

    # Accumulated agent context log: (sender, text, timestamp)
    _agent_context_log: list[tuple[str, str, float]] = []
    _agent_context_seen: set[str] = set()  # dedup keys
    _MAX_CONTEXT_MESSAGES = 8
    _MAX_CONTEXT_CHARS = 1400
    _MAX_CONTEXT_ENTRY_CHARS = 180
    _OPENCLAW_FALLBACK = "Sorry, I'm having trouble right now."
    _OPENCLAW_TIMEOUT_FALLBACK = "Sorry, that timed out. Please try again."
    _EMPTY_REPLY_FALLBACK = "Sorry, I lost the reply. Please try again."
    _VISION_FALLBACK = "Sorry, I had trouble checking that."
    _LOW_VALUE_CONTEXT_LINES = {
        greeting.lower() for greeting in _GREETINGS.values()
    } | {
        _OPENCLAW_FALLBACK.lower(),
        _OPENCLAW_TIMEOUT_FALLBACK.lower(),
        _EMPTY_REPLY_FALLBACK.lower(),
        _VISION_FALLBACK.lower(),
        "sorry, had a brief issue.",
    }
    _VISION_FAILURE_PREFIXES = (
        "sorry, i had trouble seeing",
        "sorry, the vision request timed out",
    )

    # Persistent cache for reconnect survival
    _CONTEXT_CACHE_PATH = f"/srv/project/livekit-collab/agent/.cache/{room_name}-{agent_name.lower()}-context.jsonl"

    def _normalize_context_text(text: str) -> str:
        """Normalize and cap a context line before storing or injecting it."""
        return normalize_context_text(text, _MAX_CONTEXT_ENTRY_CHARS)

    def _should_store_context_message(text: str) -> bool:
        """Skip empty and obviously low-value lines from persistent room context."""
        return should_store_context_message(
            text,
            low_value_lines=_LOW_VALUE_CONTEXT_LINES,
            max_entry_chars=_MAX_CONTEXT_ENTRY_CHARS,
        )

    def _store_context_message(
        sender: str,
        text: str,
        *,
        persist: bool = True,
        ts: float | None = None,
    ) -> None:
        """Store a deduplicated, bounded context line for future OpenClaw calls."""
        normalized = _normalize_context_text(text)
        if not _should_store_context_message(normalized):
            return

        dedup_key = f"{sender}:{normalized[:100]}"
        if dedup_key in _agent_context_seen:
            return

        timestamp = ts if ts is not None else time.monotonic()
        _agent_context_seen.add(dedup_key)
        _agent_context_log.append((sender, normalized, timestamp))
        if len(_agent_context_log) > _MAX_CONTEXT_MESSAGES:
            del _agent_context_log[:-_MAX_CONTEXT_MESSAGES]

        if not persist:
            return

        try:
            with open(_CONTEXT_CACHE_PATH, "a") as f:
                f.write(json.dumps({"s": sender, "t": normalized, "ts": timestamp}) + "\n")
        except Exception:
            pass

    try:
        os.makedirs(os.path.dirname(_CONTEXT_CACHE_PATH), exist_ok=True)
        with open(_CONTEXT_CACHE_PATH, "r") as f:
            for line in f:
                entry = json.loads(line)
                _store_context_message(
                    entry["s"],
                    entry.get("t", ""),
                    persist=False,
                    ts=entry.get("ts", 0),
                )
        logger.info(f"[{agent_name}] Loaded {len(_agent_context_log)} cached context messages")
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug(f"[{agent_name}] Failed to load context cache", exc_info=True)

    # Dedup for sent chat messages
    _recently_sent: dict[str, float] = {}

    # Dedup: texts already processed from voice (so text_input_cb skips them)
    _voice_processed: dict[str, float] = {}
    _VOICE_DEDUP_WINDOW = 10.0  # seconds

    # Dedup: texts already processed from any topic (prevents cross-topic duplicates)
    _processed_texts: dict[str, float] = {}
    _TEXT_DEDUP_WINDOW = 10.0  # seconds

    # Mutable container for identity
    _identity_ref: list[str] = [""]
    _ATTR_UNSET = object()
    _agent_attr_state: dict[str, str] = {
        "agent_name": agent_name,
        "agent_state": "idle",
        "agent_activity": "idle",
        "agent_status_text": "Starting up",
        "agent_last_activity_at": str(int(time.time() * 1000)),
        "agent_error_text": "",
        "claimed_at": "",
    }

    # OpenClaw session ID for persistent conversation
    _session_id = f"livekit-{room_name}"

    # Voice preamble injected on first OpenClaw call
    _VOICE_PREAMBLE = (
        "[VOICE ROOM RULES — READ CAREFULLY]: "
        "You ARE speaking through LiveKit right now. Your words ARE being spoken aloud via TTS. "
        "You are ALREADY connected to the voice room. There is NOTHING to set up or integrate. "
        "When you reply, your text is converted to speech and played to everyone in the room. "
        "This is ALREADY happening. Just talk.\n"
        "Keep EVERY response to 1-2 sentences, max 25 words. "
        "NO markdown, NO lists. Talk naturally like a human on a phone call. "
        "Other agents and humans are in this room. Always speak English.\n"
        "CRITICAL PROHIBITIONS:\n"
        "- Do NOT say you lack LiveKit integration — you ARE in LiveKit right now.\n"
        "- Do NOT reference Discord, Telegram, Slack, bash scripts, or any external tools unless the human explicitly asks.\n"
        "- Do NOT ask for approvals, exec permissions, API keys, or credentials.\n"
        "- Do NOT try to run commands or scripts to 'connect' to anything — you are ALREADY connected.\n"
        "- To talk to another agent, just speak — they are right here in the room."
    )
    _preamble_sent: list[bool] = [False]

    def _clean_attr_text(text: str, max_len: int = 120) -> str:
        """Collapse whitespace and cap participant attribute text."""
        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) > max_len:
            normalized = normalized[: max_len - 3].rstrip() + "..."
        return normalized

    async def _set_agent_meta(
        *,
        state=_ATTR_UNSET,
        activity=_ATTR_UNSET,
        status=_ATTR_UNSET,
        error=_ATTR_UNSET,
        claimed_at=_ATTR_UNSET,
        touch: bool = False,
    ):
        """Update agent observability attrs without changing the room/session model."""
        updates: dict[str, str] = {}

        if state is not _ATTR_UNSET:
            updates["agent_state"] = _clean_attr_text(str(state), 32)
        if activity is not _ATTR_UNSET:
            updates["agent_activity"] = _clean_attr_text(str(activity), 32)
        if status is not _ATTR_UNSET:
            updates["agent_status_text"] = _clean_attr_text(str(status), 96)
        if error is not _ATTR_UNSET:
            updates["agent_error_text"] = _clean_attr_text(str(error), 120)
        if claimed_at is not _ATTR_UNSET:
            updates["claimed_at"] = "" if claimed_at is None else str(claimed_at)
        if touch:
            updates["agent_last_activity_at"] = str(int(time.time() * 1000))

        if not updates:
            return

        _agent_attr_state.update(updates)
        if not _identity_ref[0]:
            return

        try:
            await ctx.room.local_participant.set_attributes(_agent_attr_state.copy())
        except Exception:
            logger.debug(f"[{agent_name}] Failed to update participant attrs", exc_info=True)

    def _build_room_context() -> str:
        """Build accumulated agent messages as context for OpenClaw."""
        return build_room_context(
            _agent_context_log,
            max_messages=_MAX_CONTEXT_MESSAGES,
            max_chars=_MAX_CONTEXT_CHARS,
            max_entry_chars=_MAX_CONTEXT_ENTRY_CHARS,
        )

    async def _ask_openclaw(human_text: str, sender: str) -> str:
        """Single OpenClaw call with room context and brevity enforcement."""
        context = _build_room_context()
        message = f"{context}[{sender}]: {human_text}\n\n[Voice reply: 1-2 sentences, <=25 words, plain text.]"
        preamble_added = False

        # Prepend voice preamble on first call
        if not _preamble_sent[0]:
            _preamble_sent[0] = True
            message = f"{_VOICE_PREAMBLE}\n\n{message}"
            preamble_added = True

        prompt_chars = len(message)
        context_chars = len(context)
        context_messages = min(len(_agent_context_log), _MAX_CONTEXT_MESSAGES)

        await _set_agent_meta(
            activity="calling_openclaw",
            status=f"Calling OpenClaw about: {human_text}",
            error="",
            touch=True,
        )
        logger.info(
            f"[{agent_name}] → OpenClaw: sender={sender} prompt_chars={prompt_chars} "
            f"context_chars={context_chars} context_msgs={context_messages} "
            f"preamble={'yes' if preamble_added else 'no'}"
        )
        started = time.monotonic()
        try:
            result = await send_to_openclaw(
                agent_name, message, session_id=_session_id, timeout=30,
            )
        except Exception as exc:
            await _set_agent_meta(
                activity="error",
                status="Bridge request failed",
                error="Bridge failure",
                touch=True,
            )
            logger.exception(f"[{agent_name}] OpenClaw bridge raised unexpectedly")
            return _OPENCLAW_FALLBACK

        duration_ms = int((time.monotonic() - started) * 1000)
        classification = classify_openclaw_result(
            result,
            openclaw_fallback=_OPENCLAW_FALLBACK,
            timeout_fallback=_OPENCLAW_TIMEOUT_FALLBACK,
            empty_reply_fallback=_EMPTY_REPLY_FALLBACK,
        )

        if not classification["ok"]:
            await _set_agent_meta(
                activity="error",
                status=str(classification["status"]),
                error=str(classification["safe_error"]),
                touch=True,
            )
            logger.error(
                f"[{agent_name}] OpenClaw failed after {duration_ms}ms: "
                f"{classification['result_text'] or 'no error text'}"
            )
            return str(classification["spoken_text"])

        await _set_agent_meta(
            activity="thinking",
            status="Reply ready",
            error="",
            touch=True,
        )
        logger.info(
            f"[{agent_name}] ← OpenClaw: reply_chars={len(str(classification['spoken_text']))} duration_ms={duration_ms}"
        )
        return str(classification["spoken_text"])

    async def _get_response(text: str, sender: str) -> str:
        """Route to vision (direct Claude API) or OpenClaw based on intent."""
        if is_vision_request(text):
            source = "screen" if "screen" in text.lower() else "camera"
            await _set_agent_meta(
                activity="vision_processing",
                status=f"Checking {source}",
                error="",
                touch=True,
            )
            try:
                jpeg = await capture_frame(ctx.room, source=source)
            except Exception:
                await _set_agent_meta(
                    activity="error",
                    status="Vision capture failed",
                    error="Vision capture failed",
                    touch=True,
                )
                logger.exception(f"[{agent_name}] Vision capture failed")
                return _VISION_FALLBACK
            if jpeg is None:
                await _set_agent_meta(
                    activity="thinking",
                    status=f"No {source} visible",
                    error="",
                    touch=True,
                )
                if source == "screen":
                    return "I don't see a screen share. Are you sharing your screen?"
                return "I can't see you right now — is your camera on?"
            try:
                response = await ask_claude_vision(jpeg, text, agent_name)
            except Exception:
                await _set_agent_meta(
                    activity="error",
                    status="Vision request failed",
                    error="Vision request failed",
                    touch=True,
                )
                logger.exception(f"[{agent_name}] Vision request raised unexpectedly")
                return _VISION_FALLBACK

            cleaned = (response or "").strip()
            if not cleaned:
                await _set_agent_meta(
                    activity="error",
                    status="Vision reply was empty",
                    error="Vision reply was empty",
                    touch=True,
                )
                logger.error(f"[{agent_name}] Vision returned empty reply")
                return _VISION_FALLBACK

            cleaned_lower = cleaned.lower()
            if is_vision_failure_text(cleaned, _VISION_FAILURE_PREFIXES):
                status = "Vision timed out" if "timed out" in cleaned_lower else "Vision request failed"
                error = "Vision timed out" if "timed out" in cleaned_lower else "Vision request failed"
                await _set_agent_meta(
                    activity="error",
                    status=status,
                    error=error,
                    touch=True,
                )
                logger.warning(f"[{agent_name}] Vision fallback reply: {cleaned}")
                return _VISION_FALLBACK

            await _set_agent_meta(
                activity="thinking",
                status="Vision reply ready",
                error="",
                touch=True,
            )
            return cleaned
        return await _ask_openclaw(text, sender)

    # Regex for direct address: name at start, or "hey/hi/yo name", or "name,"
    # This prevents false positives like "I see Loki here" triggering Loki
    def _is_directly_addressed(text: str, name: str) -> bool:
        """Check if text directly addresses `name` (not just mentions it)."""
        return is_directly_addressed(text, name)

    def _addressed_to_other(text: str) -> bool:
        """Return True if text directly addresses a different agent (not us)."""
        my_name_lower = agent_name.lower()
        for name in _AGENT_NAMES:
            if name != my_name_lower and _is_directly_addressed(text, name):
                if not _is_directly_addressed(text, my_name_lower):
                    return True
        return False

    def _addressed_to_us(text: str) -> bool:
        """Return True if text directly addresses us."""
        return _is_directly_addressed(text, agent_name)

    def _mentions_us(text: str) -> bool:
        """Return True if our name appears anywhere in the text."""
        return mentions_name(text, agent_name)

    # Human messages: looser check — any mention of our name counts
    def _human_mentions_us(text: str) -> bool:
        return agent_name.lower() in text.lower()

    def _human_mentions_both(text: str) -> bool:
        """Return True if text mentions both us AND another agent."""
        t = text.lower()
        mentions_us = agent_name.lower() in t
        mentions_other = any(name in t for name in _AGENT_NAMES if name != agent_name.lower())
        return mentions_us and mentions_other

    def _human_mentions_other(text: str) -> bool:
        my_name_lower = agent_name.lower()
        for name in _AGENT_NAMES:
            if name != my_name_lower and name in text.lower():
                if my_name_lower not in text.lower():
                    return True
        return False

    def _get_human_name() -> str:
        """Find the name of the human participant in the room."""
        for p in ctx.room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
                return p.name or p.identity
        return "someone"

    # Cascade breaker: track when we last replied to an agent message
    _last_agent_reply_time: list[float] = [0.0]
    _AGENT_REPLY_COOLDOWN = 2.5  # seconds — allow short exchanges without rapid double-fire
    _AUTO_AGENT_CHAIN_TURNS: list[int] = [0]
    _AUTO_AGENT_CHAIN_LAST_TS: list[float] = [0.0]
    _AUTO_AGENT_CHAIN_LIMIT = 4
    _AUTO_AGENT_CHAIN_WINDOW = 35.0

    def _reset_auto_agent_chain() -> None:
        _AUTO_AGENT_CHAIN_TURNS[0] = 0
        _AUTO_AGENT_CHAIN_LAST_TS[0] = 0.0

    def _can_continue_auto_agent_chain(now: float) -> bool:
        last_ts = _AUTO_AGENT_CHAIN_LAST_TS[0]
        if not last_ts or now - last_ts > _AUTO_AGENT_CHAIN_WINDOW:
            _reset_auto_agent_chain()
        return _AUTO_AGENT_CHAIN_TURNS[0] < _AUTO_AGENT_CHAIN_LIMIT

    def _record_auto_agent_chain_turn(now: float) -> None:
        if not _AUTO_AGENT_CHAIN_LAST_TS[0] or now - _AUTO_AGENT_CHAIN_LAST_TS[0] > _AUTO_AGENT_CHAIN_WINDOW:
            _AUTO_AGENT_CHAIN_TURNS[0] = 0
        _AUTO_AGENT_CHAIN_LAST_TS[0] = now
        _AUTO_AGENT_CHAIN_TURNS[0] += 1

    def _is_other_agent_busy() -> bool:
        mid = _identity_ref[0]
        for p in ctx.room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT and p.identity != mid:
                state = p.attributes.get("agent_state", "idle")
                if state in ("speaking", "thinking"):
                    return True
                # Also check if the agent recently claimed a generic message
                # (covers the case where Laira finishes before Loki checks)
                claimed_at = p.attributes.get("claimed_at")
                if claimed_at:
                    try:
                        if time.time() - float(claimed_at) < 10.0:
                            return True
                    except (ValueError, TypeError):
                        pass
        return False

    async def _wait_for_other_agent_idle(timeout: float = 10.0):
        waited = 0.0
        while _is_other_agent_busy() and waited < timeout:
            await asyncio.sleep(0.3)
            waited += 0.3

    async def _say_and_echo(text: str, *, preserve_error: bool = False):
        """Speak text via TTS and echo to chat channels."""
        await _set_agent_meta(
            activity="speaking",
            status="Speaking response",
            touch=True,
        )
        await session.say(text)
        await _send_chat_echo(text)
        await _set_agent_meta(
            state="idle",
            activity="idle",
            status="Awaiting next input",
            error=_ATTR_UNSET if preserve_error else "",
            claimed_at=None,
            touch=True,
        )

    async def _send_chat_echo(text: str):
        """Echo a response to human chat and agent chat channels."""
        now = time.monotonic()
        last_sent = _recently_sent.get(text)
        if last_sent and now - last_sent < 10.0:
            return
        _recently_sent[text] = now
        # Prune old entries
        for k in [k for k, v in _recently_sent.items() if now - v > 30.0]:
            del _recently_sent[k]

        # Small delay to let messages arrive in order
        await asyncio.sleep(0.3)

        try:
            await ctx.room.local_participant.send_text(
                text, topic="lk.chat",
                attributes={"lk.chat.sender_name": agent_name},
            )
            await ctx.room.local_participant.send_text(
                text, topic="lk.agent.chat",
                attributes={"lk.chat.sender_name": agent_name},
            )
            logger.debug(f"[{agent_name}] Sent chat: {text[:80]}...")
        except Exception:
            logger.debug(f"[{agent_name}] Failed to send chat reply", exc_info=True)

        # Store our own response in context log so the other agent sees both sides
        _store_context_message(agent_name, text)

    # --- Shared handler for human input (voice or text) ---
    async def _broadcast_thinking():
        """Broadcast 'thinking' state immediately via participant attributes."""
        await _set_agent_meta(
            state="thinking",
            activity="thinking",
            status="Thinking",
            error="",
            claimed_at=time.time(),
            touch=True,
        )

    async def _broadcast_idle():
        """Broadcast 'idle' state via participant attributes."""
        await _set_agent_meta(
            state="idle",
            activity="idle",
            status="Awaiting next input",
            error="",
            claimed_at=None,
            touch=True,
        )

    async def _get_spoken_response(text: str, sender: str) -> tuple[str, bool]:
        """Get a reply while guaranteeing a spoken fallback on failure/empties."""
        try:
            response = await _get_response(text, sender)
        except Exception:
            await _set_agent_meta(
                activity="error",
                status="Response generation failed",
                error="Response generation failed",
                touch=True,
            )
            logger.exception(f"[{agent_name}] Response generation crashed")
            return _OPENCLAW_FALLBACK, True

        cleaned, used_fallback = ensure_spoken_response_text(response, _EMPTY_REPLY_FALLBACK)
        if not used_fallback:
            return cleaned, False

        await _set_agent_meta(
            activity="error",
            status="Reply was empty",
            error="Reply was empty",
            touch=True,
        )
        logger.error(f"[{agent_name}] Response pipeline produced empty text")
        return _EMPTY_REPLY_FALLBACK, True

    async def _handle_human_input(text: str, sender: str):
        """Process human input through turn-taking → OpenClaw → TTS."""
        # Fix STT misrecognitions of agent names before any name checks
        text = _correct_names(text)
        _reset_auto_agent_chain()

        # Skip if human addresses a different agent (not us)
        if _human_mentions_other(text):
            logger.debug(f"[{agent_name}] Skipping — human addressed other agent")
            await _broadcast_idle()
            return

        # If human mentions ONLY us (not both agents), respond immediately
        if _human_mentions_us(text) and not _human_mentions_both(text):
            await session.interrupt()
            await _broadcast_thinking()
            response, preserve_error = await _get_spoken_response(text, sender)
            await _say_and_echo(response, preserve_error=preserve_error)
            return

        # Both agents mentioned, or generic message: staggered turn-taking
        # When both are mentioned, both should respond but sequentially
        if _human_mentions_both(text):
            # Both addressed: stagger but both respond (Laira first, then Loki)
            delay = {"laira": 0.5, "loki": 3.5}.get(agent_name.lower(), 1.0)
            await asyncio.sleep(delay)
            # Wait for the other agent to finish speaking before we start
            if _is_other_agent_busy():
                await _wait_for_other_agent_idle(timeout=15.0)
                await asyncio.sleep(0.5)
            await session.interrupt()
            await _broadcast_thinking()
            response, preserve_error = await _get_spoken_response(text, sender)
            await _say_and_echo(response, preserve_error=preserve_error)
            return

        # Generic message (no names): staggered delay, only one responds
        delay = {"laira": 0.5, "loki": 3.5}.get(agent_name.lower(), 1.0)
        await asyncio.sleep(delay)
        if _is_other_agent_busy():
            logger.debug(f"[{agent_name}] Skipping — other agent already responding")
            await _broadcast_idle()
            return

        # Claim the turn by broadcasting "thinking" IMMEDIATELY
        await _broadcast_thinking()
        # Small grace period for the other agent to see our claim
        await asyncio.sleep(0.5)
        # Double-check: if other agent also claimed, Loki yields (deterministic tiebreak)
        if _is_other_agent_busy() and agent_name.lower() != "laira":
            logger.debug(f"[{agent_name}] Skipping — other agent also claimed, yielding")
            await _broadcast_idle()
            return

        await session.interrupt()
        response, preserve_error = await _get_spoken_response(text, sender)
        await _say_and_echo(response, preserve_error=preserve_error)

    # --- Text input callback (human typed chat only — voice goes through user_input_transcribed) ---
    async def _chat_input_cb(sess: AgentSession, ev):
        # Skip messages from agent participants (only handle human chat)
        if ev.participant and ev.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return

        sender = ev.participant.name or ev.participant.identity if ev.participant else "someone"
        text = ev.text

        # Skip if this text was already processed from voice input (dedup)
        now = time.monotonic()
        voice_ts = _voice_processed.get(text.strip().lower())
        if voice_ts and now - voice_ts < _VOICE_DEDUP_WINDOW:
            logger.debug(f"[{agent_name}] Skipping chat — already processed from voice: {text[:40]}")
            return

        await _set_agent_meta(
            state="listening",
            activity="listening",
            status=f"Reading chat from {sender}",
            error="",
            touch=True,
        )
        logger.info(f"[{agent_name}] Chat from {sender}: {text}")
        await _handle_human_input(text, sender)

    # --- Agent-to-agent text chat handler ---
    async def _handle_agent_chat_async(reader: rtc.TextStreamReader, sender_identity: str):
        mid = _identity_ref[0]
        if sender_identity == mid:
            return

        text_parts = []
        async for chunk in reader:
            text_parts.append(chunk)
        text = "".join(text_parts).strip()

        if not text or len(text) < 2:
            return

        # Dedup: skip if we already processed this exact text recently
        # (covers same message arriving via both lk.chat and lk.agent.chat)
        dedup_key = f"{sender_identity}:{text.lower().strip()}"
        now = time.monotonic()
        prev_ts = _processed_texts.get(dedup_key)
        if prev_ts and now - prev_ts < _TEXT_DEDUP_WINDOW:
            logger.debug(f"[{agent_name}] Skipping duplicate agent chat: {text[:40]}")
            return
        _processed_texts[dedup_key] = now
        # Prune old entries
        for k in [k for k, v in _processed_texts.items() if now - v > _TEXT_DEDUP_WINDOW * 3]:
            del _processed_texts[k]

        sender_name = sender_identity
        p = ctx.room.remote_participants.get(sender_identity)
        if p:
            sender_name = p.name or p.attributes.get("agent_name") or p.identity

        # Skip human messages (handled by text_input_cb)
        if p and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
            return

        logger.info(f"[{agent_name}] Agent chat from {sender_name}: {text[:80]}")

        # Store for context (always, even if we don't reply) — append-only with dedup
        _store_context_message(sender_name, text)

        trigger = classify_agent_turn_trigger(text, agent_name)
        if not trigger:
            logger.debug(f"[{agent_name}] Agent text from {sender_name}: noted (no trigger)")
            return

        now = time.monotonic()
        if not _can_continue_auto_agent_chain(now):
            logger.debug(f"[{agent_name}] Agent reply suppressed (chain limit reached)")
            return

        # Cascade breaker: don't reply to agents more than once per cooldown window
        if now - _last_agent_reply_time[0] < _AGENT_REPLY_COOLDOWN:
            logger.debug(f"[{agent_name}] Agent reply suppressed (cascade cooldown)")
            return
        _last_agent_reply_time[0] = now

        logger.info(f"[{agent_name}] Triggered by {sender_name} via {trigger} mention, will respond")
        await _wait_for_other_agent_idle(timeout=10.0)
        await asyncio.sleep(0.6)
        await session.interrupt()
        await _broadcast_thinking()
        response, preserve_error = await _get_spoken_response(text, sender_name)
        _record_auto_agent_chain_turn(time.monotonic())
        await _say_and_echo(response, preserve_error=preserve_error)

    def _handle_agent_chat(reader: rtc.TextStreamReader, sender_identity: str):
        asyncio.create_task(_handle_agent_chat_async(reader, sender_identity))

    # --- Cleanup ---
    async def _cleanup():
        logger.info(f"Agent '{agent_name}' shutting down in room '{room_name}'")
        try:
            await session.aclose()
        except Exception:
            pass
        try:
            await ctx.room.disconnect()
        except Exception:
            pass

    ctx.add_shutdown_callback(_cleanup)

    try:
        # Check if another instance of this agent is already in the room
        for p in ctx.room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                pname = p.name or p.attributes.get("agent_name") or ""
                if pname.lower() == agent_name.lower():
                    logger.warning(f"[{agent_name}] Another instance already in room, disconnecting")
                    ctx.shutdown(reason=f"Duplicate {agent_name}")
                    return

        # AgentSession needs an Agent object — pass a minimal one with no instructions
        # (all intelligence comes from OpenClaw)
        agent = Agent(instructions="You are a voice agent. Respond briefly.")

        await session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                text_enabled=True,
                text_input_cb=_chat_input_cb,
                # Keep the session alive across human leave/rejoin cycles so
                # typed chat and observability hooks continue to work in long-lived rooms.
                close_on_disconnect=False,
            ),
        )

        # Set identity and attributes
        my_identity = ctx.room.local_participant.identity
        _identity_ref[0] = my_identity
        await ctx.room.local_participant.set_name(agent_name)
        await _set_agent_meta(
            state="idle",
            activity="idle",
            status="Waiting in room",
            error="",
            claimed_at=None,
            touch=True,
        )
        logger.info(f"[{agent_name}] Session started (identity: {my_identity})")

        # Broadcast agent state via participant attributes
        @session.on("agent_state_changed")
        def _broadcast_agent_state(event):
            new_state = str(getattr(event, "new_state", getattr(event, "state", "unknown")))
            logger.debug(f"[{agent_name}] State broadcast: {new_state}")
            asyncio.create_task(_set_agent_meta(state=new_state))

        # Track notified agents
        _notified_agents: set[str] = set()

        # Notify about agents already in the room
        for p in ctx.room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT and p.identity != my_identity:
                pname = p.name or p.attributes.get("agent_name") or p.identity
                _notified_agents.add(p.identity)
                logger.info(f"[{agent_name}] Detected agent already in room: {pname}")

        # Listen for new participants
        def _on_participant_connected(participant: rtc.RemoteParticipant):
            if participant.identity == my_identity:
                return
            if participant.identity in _notified_agents:
                return
            if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                pname = participant.name or participant.attributes.get("agent_name") or participant.identity
                _notified_agents.add(participant.identity)
                logger.info(f"[{agent_name}] Agent joined room: {pname}")

        ctx.room.on("participant_connected", _on_participant_connected)

        # Register agent-to-agent text chat
        try:
            ctx.room.register_text_stream_handler("lk.agent.chat", _handle_agent_chat)
            logger.info(f"[{agent_name}] Agent-to-agent text chat enabled")
        except ValueError:
            logger.debug(f"[{agent_name}] Agent chat handler already registered")

        # --- Voice input: human speaks → STT transcribes → process + echo to chat ---
        @session.on("user_input_transcribed")
        def _on_user_input_transcribed(event):
            if not event.is_final:
                return
            text = event.transcript.strip()
            if not text or len(text) < 2:
                return

            sender = _get_human_name()
            logger.info(f"[{agent_name}] Voice transcription from {sender}: {text}")
            asyncio.create_task(
                _set_agent_meta(
                    state="listening",
                    activity="listening",
                    status=f"Heard {sender}",
                    error="",
                    touch=True,
                )
            )

            # Mark as processed so text_input_cb doesn't double-handle
            _voice_processed[text.lower()] = time.monotonic()
            # Prune old entries
            now = time.monotonic()
            for k in [k for k, v in _voice_processed.items() if now - v > _VOICE_DEDUP_WINDOW]:
                del _voice_processed[k]

            asyncio.create_task(_process_voice_input(text, sender))

        async def _process_voice_input(text: str, sender: str):
            """Echo voice transcription to chat (so it's visible) and process it."""
            # Fix STT misrecognitions of agent names
            text = _correct_names(text)

            # Echo the transcription to lk.chat with the human's name so it shows in UI
            # Only the primary agent (Laira) echoes to avoid duplicate chat messages
            if agent_name.lower() == "laira":
                try:
                    await ctx.room.local_participant.send_text(
                        text, topic="lk.chat",
                        attributes={
                            "lk.chat.sender_name": sender,
                            "transcription": "true",
                        },
                    )
                    logger.info(f"[{agent_name}] Echoed voice transcription to chat: '{text}' as {sender}")
                except Exception:
                    logger.warning(f"[{agent_name}] Failed to echo voice transcription to chat", exc_info=True)

            # Process through the standard human input handler
            await _handle_human_input(text, sender)

        # Static greeting — zero LLM calls
        greeting_delay = {"laira": 1.0, "loki": 4.0}.get(agent_name.lower(), 2.0)
        await asyncio.sleep(greeting_delay)
        greeting = _GREETINGS.get(agent_name.lower(), f"Hey, {agent_name} here!")
        logger.info(f"[{agent_name}] Static greeting: {greeting}")
        await _say_and_echo(greeting)

    except Exception as e:
        try:
            await _set_agent_meta(
                activity="error",
                status="Agent crashed",
                error=str(e),
                touch=True,
            )
        except Exception:
            pass
        logger.exception(f"Agent '{agent_name}' crashed in room '{room_name}'")
        ctx.shutdown(reason=f"{agent_name} crashed")


if __name__ == "__main__":
    cli.run_app(server)
