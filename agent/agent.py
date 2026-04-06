"""Agora voice agent — real-time voice I/O for the Agora platform.

Architecture:
  Human speaks (mic) → VAD → STT → user_input_transcribed → ACP bridge → session.say() → TTS
  Human types (chat) → text_input_cb → ACP bridge → session.say() → TTS
  Agent-to-agent: text chat on lk.agent.chat (context sharing + mention-triggered replies)
  Cross-session: ACP Event Bus for shared context across Agora/Telegram/Discord

Intelligence comes from Hermes (Laira) and OpenClaw (Loki) gateways via the ACP bridge.
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
from openclaw_llm_plugin import NoOpLLM

# ACP bridge (streaming HTTP to Hermes gateway) vs legacy docker-exec bridge.
# ACP requires the Hermes API server running inside the container.
# Agents on OpenClaw (e.g. Loki) must use ACP_ENABLED=false to keep docker exec.
_ACP_ENABLED = os.environ.get("ACP_ENABLED", "true").lower() in ("true", "1", "yes")

# Always import legacy bridge (it's stdlib-only, zero cost) so both paths work
from openclaw_bridge import send_to_openclaw

if _ACP_ENABLED:
    from acp_bridge import stream_from_gateway, send_to_gateway, close_session as _acp_close
    from acp_protocol import ACPMessage, ACPResponseChunk, ChunkType, MessageType
from acp_bus_client import AcpBusClient
from runtime_utils import (
    build_room_context,
    classify_openclaw_result,
    ensure_spoken_response_text,
    is_directly_addressed,
    is_group_address,
    is_stop_command,
    is_vision_failure_text,
    mentions_name,
    normalize_context_text,
    parse_turn_count,
    should_store_context_message,
)
from vision import is_vision_request, capture_frame, ask_claude_vision

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

logger = logging.getLogger("agora-agent")

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

# Known agents for turn-taking (loaded from registry)
from agent_registry import (
    agent_names as _get_agent_names,
    get_greeting as _get_greeting,
    get_delay as _get_delay,
    is_primary as _is_primary,
    get_gateway_urls as _get_gateway_urls,
    get_container_map as _get_container_map,
)
_AGENT_NAMES = _get_agent_names()

# Common STT misrecognitions → correct name
# Auto-generate from agent names if not customized
_NAME_CORRECTIONS = json.loads(os.environ.get("AGENT_NAME_CORRECTIONS", "{}")) or {
    "lucky": "loki", "lockey": "loki", "locky": "loki",
    "laky": "loki", "loca": "loki", "lokay": "loki",
    "laura": "laira", "lyra": "laira", "lara": "laira",
    "lair": "laira", "layer": "laira", "layra": "laira",
}


# Voice relay prefix: primary agent relays voice transcriptions to non-primary agents
_VOICE_RELAY_RE = re.compile(r'^\[VOICE_RELAY:(.+?)\]\s*(.+)$', re.DOTALL)

# Turn relay prefix: automatic turn-taking between agents
_TURN_RELAY_RE = re.compile(r'^\[TURN_RELAY:(\d+)/(\d+)\]\s*(.+)$', re.DOTALL)


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

# Static greetings — zero LLM calls (loaded from registry)
_GREETINGS = {name: _get_greeting(name) for name in _AGENT_NAMES}

server = AgentServer()


# Per-agent voice defaults (loaded from registry)
from agent_registry import get_voice as _get_voice_reg
_DEFAULT_VOICES: dict[str, str] = {name: _get_voice_reg(name) for name in _AGENT_NAMES}


def _resolve_voice(name: str) -> str:
    """Resolve TTS voice: EDGE_TTS_VOICE_{NAME} > EDGE_TTS_VOICE > per-agent default."""
    agent_key = f"EDGE_TTS_VOICE_{name.upper()}"
    specific = os.environ.get(agent_key, "").strip()
    if specific:
        return specific
    generic = os.environ.get("EDGE_TTS_VOICE", "").strip()
    if generic:
        return generic
    return _DEFAULT_VOICES.get(name.lower(), "en-US-AriaNeural")


@server.rtc_session(agent_name=agent_name)
async def entrypoint(ctx: JobContext):
    voice = _resolve_voice(agent_name)
    room_name = re.sub(r'[^a-zA-Z0-9_-]', '', ctx.room.name)[:64]

    logger.info(f"Starting agent '{agent_name}' in room '{room_name}' (voice={voice})")

    # ACP Event Bus — shared cross-session context
    _bus = AcpBusClient()
    _bus_topic = f"room:{room_name}"

    async def _bus_event_handler(topic: str, event: dict) -> None:
        """Handle incoming bus events from other sessions/agents."""
        # Store events from other agents/sessions into our local context
        speaker = event.get("speaker", "")
        content = event.get("content", "")
        evt_agent = event.get("agent", "")
        if content and evt_agent != agent_name.lower():
            _store_context_message(speaker, content, persist=False)

    _bus.on_event = _bus_event_handler

    async def _connect_bus() -> None:
        ok = await _bus.connect_with_retry(max_attempts=3)
        if ok:
            await _bus.subscribe([_bus_topic, f"agent:{agent_name.lower()}"])
            logger.info(f"[{agent_name}] Connected to ACP Event Bus, subscribed to {_bus_topic}")
        else:
            logger.warning(f"[{agent_name}] ACP Event Bus unavailable — running without cross-session context")

    asyncio.create_task(_connect_bus())

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
    _EMPTY_REPLY_FALLBACK = "Hmm, give me a second, that one took too long. Try again?"
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
    _cache_dir = os.path.expanduser(os.getenv("AGORA_CACHE_DIR", "~/.agora/cache"))
    _CONTEXT_CACHE_PATH = f"{_cache_dir}/{room_name}-{agent_name.lower()}-context.jsonl"

    def _sanitize_name(name: str) -> str:
        """Strip control characters and limit participant name length."""
        return re.sub(r'[\x00-\x1f\x7f]', '', name or "unknown")[:64]

    # Regex patterns for TTS sanitization
    _CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```', re.MULTILINE)
    _INLINE_CODE_RE = re.compile(r'`[^`]+`')
    _URL_RE = re.compile(r'https?://\S+')
    _TERMINAL_LINE_RE = re.compile(r'^[\s]*[$#>].*$', re.MULTILINE)
    _CURL_CMD_RE = re.compile(r'curl\s+-?\S.*', re.IGNORECASE)
    _JSON_BLOCK_RE = re.compile(r'\{[^}]*"[^"]*"[^}]*\}')
    _MARKDOWN_HEADER_RE = re.compile(r'^#{1,6}\s+', re.MULTILINE)
    _MARKDOWN_BOLD_RE = re.compile(r'\*\*([^*]+)\*\*')
    _MARKDOWN_LIST_RE = re.compile(r'^\s*[-*]\s+', re.MULTILINE)
    # Tool/API output patterns
    _TOOL_CALL_RE = re.compile(r'\[?tool_(?:call|result|use)[:\s].*', re.IGNORECASE)
    _API_RESPONSE_RE = re.compile(r'"(?:status|error|message|result|data|ok)":\s*["{[\d].*')
    _PIPE_CMD_RE = re.compile(r'\b(?:grep|awk|sed|cat|echo|pip|npm|docker|git|python|bash|sh|wget)\b\s+\S.*', re.IGNORECASE)
    _HTTP_METHOD_RE = re.compile(r'\b(?:GET|POST|PUT|DELETE|PATCH)\s+(?:https?://|/\w)', re.IGNORECASE)
    _FILE_PATH_RE = re.compile(r'(?:/[\w.-]+){3,}')
    _TELEGRAM_CMD_RE = re.compile(r'(?:send_message|sendMessage|api\.telegram)\S*.*', re.IGNORECASE)

    def _sanitize_for_tts(text: str) -> str:
        """Strip code, URLs, commands, tool output, and markdown before TTS."""
        if not text:
            return text
        t = _CODE_BLOCK_RE.sub('', text)
        t = _INLINE_CODE_RE.sub('', t)
        t = _URL_RE.sub('', t)
        t = _TERMINAL_LINE_RE.sub('', t)
        t = _CURL_CMD_RE.sub('', t)
        t = _JSON_BLOCK_RE.sub('', t)
        t = _TOOL_CALL_RE.sub('', t)
        t = _API_RESPONSE_RE.sub('', t)
        t = _PIPE_CMD_RE.sub('', t)
        t = _HTTP_METHOD_RE.sub('', t)
        t = _FILE_PATH_RE.sub('', t)
        t = _TELEGRAM_CMD_RE.sub('', t)
        t = _MARKDOWN_HEADER_RE.sub('', t)
        t = _MARKDOWN_BOLD_RE.sub(r'\1', t)
        t = _MARKDOWN_LIST_RE.sub('', t)
        # Collapse whitespace
        t = re.sub(r'\n{2,}', '. ', t)
        t = re.sub(r'\s+', ' ', t).strip()
        return t

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

        # Publish to ACP Event Bus (non-blocking, best-effort)
        is_agent = sender.lower() in _AGENT_NAMES
        if _bus.connected:
            asyncio.create_task(_bus.publish(
                f"room:{room_name}",
                {
                    "type": "agent_response" if is_agent else "voice_input",
                    "agent": agent_name.lower(),
                    "speaker": sender,
                    "content": normalized,
                },
            ))

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

    # Turn-taking state: user-controlled multi-turn agent conversations
    _turn_total: list[int] = [0]       # total turns requested
    _turn_remaining: list[int] = [0]   # turns left
    _turn_initiator: list[str] = [""]  # user who requested turns


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
    _session_id = f"agora-{room_name}-{os.urandom(8).hex()}"

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

    async def _build_prompt(human_text: str, sender: str) -> tuple[str, bool]:
        """Build the full prompt with context, bus events, and voice preamble."""
        context = _build_room_context()

        # Inject recent cross-session events from the ACP bus
        bus_context = ""
        if _bus.connected:
            try:
                recent = await _bus.get_recent(_bus_topic, n=10)
                if recent:
                    lines = []
                    for evt in recent[-6:]:
                        spk = evt.get("speaker", "?")
                        ct = evt.get("content", "")[:120]
                        et = evt.get("type", "")
                        if ct:
                            label = "said" if et == "voice_input" else "responded"
                            lines.append(f"  {spk} {label}: {ct}")
                    if lines:
                        bus_context = "[Recent room activity via ACP bus]\n" + "\n".join(lines) + "\n\n"
            except Exception:
                pass

        message = f"{bus_context}{context}[{sender}]: {human_text}\n\n[Voice reply: 1-2 sentences, <=25 words, plain text.]"
        preamble_added = False
        if not _preamble_sent[0]:
            _preamble_sent[0] = True
            message = f"{_VOICE_PREAMBLE}\n\n{message}"
            preamble_added = True
        return message, preamble_added

    async def _ask_acp_streaming(human_text: str, sender: str) -> str:
        """ACP streaming call — speaks sentences progressively via TTS as they arrive."""
        message, preamble_added = await _build_prompt(human_text, sender)

        await _set_agent_meta(
            activity="calling_acp",
            status="Processing voice input",
            error="",
            touch=True,
        )
        logger.info(
            f"[{agent_name}] → ACP: sender={sender} prompt_chars={len(message)} "
            f"preamble={'yes' if preamble_added else 'no'}"
        )
        started = time.monotonic()

        acp_msg = ACPMessage(
            type=MessageType.VOICE_INPUT,
            session_id=_session_id,
            sender=sender,
            content=message,
            metadata={
                "room": room_name,
                "modality": "voice",
                "agent_name": agent_name,
            },
        )

        # Accumulate text and speak sentences as they complete.
        _sentence_buf: list[str] = []     # chars of current partial sentence
        _spoken_sentences: list[str] = [] # already spoken
        _full_parts: list[str] = []       # all text for final return

        # Sentence boundary: .!? followed by whitespace AND an uppercase letter.
        # This avoids splitting on abbreviations (Dr. Smith, U.S. history, e.g. foo).
        _SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')

        # TTS queue: sentences are spoken in a background task so streaming
        # can continue accumulating text while TTS renders the current sentence.
        _tts_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def _tts_worker() -> None:
            """Background worker: consume sentences from queue and speak them.

            Acquires the speaking turn once on the first sentence and holds it
            until all sentences are done (or the queue signals None).
            """
            _turn_held = False
            try:
                while True:
                    text = await _tts_queue.get()
                    if text is None:
                        break
                    try:
                        if not _turn_held:
                            await _acquire_speaking_turn()
                            _turn_held = True
                        await _set_agent_meta(
                            activity="speaking",
                            status="Speaking (streaming)",
                            touch=True,
                        )
                        await session.say(text)
                    except Exception:
                        logger.warning(f"[{agent_name}] TTS worker error", exc_info=True)
            finally:
                if _turn_held:
                    await _release_speaking_turn()

        _tts_task = asyncio.create_task(_tts_worker())

        def _enqueue_sentence(text: str) -> None:
            """Queue a completed sentence for TTS (non-blocking)."""
            cleaned = _sanitize_for_tts(text.strip())
            if not cleaned:
                return
            _spoken_sentences.append(cleaned)
            _tts_queue.put_nowait(cleaned)

        try:
            async for chunk in stream_from_gateway(
                agent_name, acp_msg,
                system_prompt=_build_system_prompt(),
                timeout=60,
            ):
                if chunk.type == ChunkType.TEXT_CHUNK:
                    _full_parts.append(chunk.content)
                    _sentence_buf.append(chunk.content)
                    # Check if we have complete sentences to speak
                    buf_text = "".join(_sentence_buf)
                    parts = _SENTENCE_SPLIT.split(buf_text)
                    if len(parts) > 1:
                        for sentence in parts[:-1]:
                            _enqueue_sentence(sentence)
                        _sentence_buf.clear()
                        if parts[-1].strip():
                            _sentence_buf.append(parts[-1])

                elif chunk.type == ChunkType.ERROR:
                    await _set_agent_meta(
                        activity="error",
                        status="ACP request failed",
                        error=chunk.content,
                        touch=True,
                    )
                    duration_ms = int((time.monotonic() - started) * 1000)
                    logger.error(
                        f"[{agent_name}] ACP failed after {duration_ms}ms: {chunk.content}"
                    )
                    _tts_queue.put_nowait(None)  # stop TTS worker
                    await _tts_task
                    if _spoken_sentences:
                        return "".join(s + " " for s in _spoken_sentences).strip()
                    return _OPENCLAW_FALLBACK

                elif chunk.type == ChunkType.DONE:
                    pass

            # Flush remaining buffered text, then drain TTS queue
            remaining = "".join(_sentence_buf).strip()
            if remaining:
                _enqueue_sentence(remaining)
            _tts_queue.put_nowait(None)  # signal worker to stop
            await _tts_task

        except Exception:
            _tts_queue.put_nowait(None)
            await _tts_task
            await _set_agent_meta(
                activity="error",
                status="Bridge request failed",
                error="ACP bridge failure",
                touch=True,
            )
            logger.exception(f"[{agent_name}] ACP bridge raised unexpectedly")
            if _spoken_sentences:
                return "".join(s + " " for s in _spoken_sentences).strip()
            return _OPENCLAW_FALLBACK

        full_text = "".join(_full_parts).strip()
        duration_ms = int((time.monotonic() - started) * 1000)

        if not full_text:
            await _set_agent_meta(
                activity="error",
                status="ACP reply was empty",
                error="ACP returned empty reply",
                touch=True,
            )
            logger.error(f"[{agent_name}] ACP returned empty response after {duration_ms}ms")
            return _EMPTY_REPLY_FALLBACK

        await _set_agent_meta(
            activity="thinking",
            status="Reply ready",
            error="",
            touch=True,
        )
        logger.info(
            f"[{agent_name}] ← ACP: reply_chars={len(full_text)} "
            f"sentences_streamed={len(_spoken_sentences)} duration_ms={duration_ms}"
        )
        return full_text

    async def _ask_openclaw_legacy(human_text: str, sender: str) -> str:
        """Legacy OpenClaw call via docker exec (non-streaming)."""
        message, preamble_added = await _build_prompt(human_text, sender)

        await _set_agent_meta(
            activity="calling_openclaw",
            status="Processing voice input",
            error="",
            touch=True,
        )
        logger.info(
            f"[{agent_name}] → OpenClaw: sender={sender} prompt_chars={len(message)} "
            f"preamble={'yes' if preamble_added else 'no'}"
        )
        started = time.monotonic()
        try:
            result = await send_to_openclaw(
                agent_name, message, session_id=_session_id, timeout=45,
            )
        except Exception:
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

    async def _ask_agent(human_text: str, sender: str) -> str:
        """Route to ACP (streaming) or legacy OpenClaw, with fallback."""
        if _ACP_ENABLED:
            try:
                return await _ask_acp_streaming(human_text, sender)
            except Exception:
                logger.warning(f"[{agent_name}] ACP failed, falling back to legacy bridge")
                return await _ask_openclaw_legacy(human_text, sender)
        return await _ask_openclaw_legacy(human_text, sender)

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
        return await _ask_agent(text, sender)

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
                return _sanitize_name(p.name or p.identity)
        return "someone"

    def _get_room_participants() -> list[str]:
        """List all participants in the room (humans and agents)."""
        names = []
        for p in ctx.room.remote_participants.values():
            pname = _sanitize_name(p.name or p.identity)
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                names.append(f"{pname} (agent)")
            else:
                names.append(pname)
        return names

    def _build_system_prompt() -> str:
        """Build a system prompt that grounds the agent in the Agora voice room."""
        participants = _get_room_participants()
        participant_str = ", ".join(participants) if participants else "unknown"
        return (
            f"PLATFORM CONTEXT: You are {agent_name}, currently in the "
            f"Agora voice room '{room_name}'. "
            f"This is a REAL-TIME VOICE conversation — not Telegram, not Discord, "
            f"not WhatsApp. You are speaking through Agora TTS right now. "
            f"Participants in this room: {participant_str}.\n"
            f"VOICE OUTPUT RULES — CRITICAL:\n"
            f"- Your text is spoken aloud via TTS. NEVER output code blocks, "
            f"terminal commands, URLs, API calls, curl commands, or technical output.\n"
            f"- When you use tools (send_message, exec, browser, etc.), work SILENTLY "
            f"and only speak the human-friendly result. Example: say 'Done, I sent the "
            f"message on Telegram' — NOT the curl command or API response.\n"
            f"- Never say 'curl', 'grep', 'cat', 'echo', 'python', or show command output.\n"
            f"- No markdown, no code fences, no bullet points. Speak naturally.\n"
            f"- Keep every response to 1-2 sentences, max 25 words.\n"
            f"CROSS-SESSION TOOL: You have the acp_bus_query tool available. "
            f"When asked about what happened in other sessions (Telegram, Discord), "
            f"or what other agents/users said elsewhere, you MUST call "
            f"acp_bus_query(topic='room:agora-comms') to check. "
            f"Do NOT say you can't access other sessions — USE the tool to check. "
            f"The ACP Event Bus connects all your sessions across platforms."
        )


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

    async def _acquire_speaking_turn(timeout: float = 20.0) -> bool:
        """Wait until no other agent is speaking, then claim the turn.

        Primary agent wins ties. Returns True when turn is acquired.
        """
        waited = 0.0
        while waited < timeout:
            other_speaking = False
            for p in ctx.room.remote_participants.values():
                if (p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                        and p.identity != _identity_ref[0]):
                    if p.attributes.get("agent_state", "idle") == "speaking":
                        other_speaking = True
                        break
            if not other_speaking:
                await _set_agent_meta(state="speaking", touch=True)
                await asyncio.sleep(0.15)
                # Verify no simultaneous claim
                conflict = False
                for p in ctx.room.remote_participants.values():
                    if (p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                            and p.identity != _identity_ref[0]):
                        if p.attributes.get("agent_state", "idle") == "speaking":
                            if not _is_primary(agent_name):
                                conflict = True
                            break
                if not conflict:
                    return True
                await _set_agent_meta(state="waiting", touch=True)
            await asyncio.sleep(0.3)
            waited += 0.45
        # Timeout: speak anyway to avoid deadlock
        await _set_agent_meta(state="speaking", touch=True)
        return True

    async def _release_speaking_turn():
        """Release the speaking turn so other agents can speak."""
        await _set_agent_meta(
            state="idle",
            activity="idle",
            status="Awaiting next input",
            claimed_at=None,
            touch=True,
        )

    async def _say_and_echo(text: str, *, preserve_error: bool = False, already_spoken: bool = False):
        """Speak text via TTS and echo to chat channels.

        When *already_spoken* is True (ACP streaming already spoke sentences
        progressively), skip the TTS call and only echo to chat.
        """
        if not already_spoken:
            await _acquire_speaking_turn()
            try:
                await _set_agent_meta(
                    activity="speaking",
                    status="Speaking response",
                    touch=True,
                )
                clean = _sanitize_for_tts(text)
                if clean:
                    await session.say(clean)
            finally:
                await _release_speaking_turn()
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
        """Echo a response to the UI chat (lk.chat only).

        Agent-to-agent communication uses explicit relay messages
        (VOICE_RELAY, TURN_RELAY) on lk.agent.chat, not plain echoes.
        Context sharing uses _store_context_message + ACP Event Bus.
        """
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
            logger.debug(f"[{agent_name}] Sent chat: {text[:80]}...")
        except Exception:
            logger.warning(f"[{agent_name}] Failed to send chat reply", exc_info=True)

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

    async def _get_spoken_response(text: str, sender: str) -> tuple[str, bool, bool]:
        """Get a reply while guaranteeing a spoken fallback on failure/empties.

        Returns (response_text, preserve_error, already_spoken).
        *already_spoken* is True when ACP streaming already piped text to TTS.
        """
        # Track whether ACP streaming spoke the response progressively
        _acp_streamed = _ACP_ENABLED and not is_vision_request(text)

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
            return _OPENCLAW_FALLBACK, True, False

        cleaned, used_fallback = ensure_spoken_response_text(response, _EMPTY_REPLY_FALLBACK)
        if not used_fallback:
            return cleaned, False, _acp_streamed

        await _set_agent_meta(
            activity="error",
            status="Reply was empty",
            error="Reply was empty",
            touch=True,
        )
        logger.error(f"[{agent_name}] Response pipeline produced empty text")
        return _EMPTY_REPLY_FALLBACK, True, False

    async def _send_turn_relay(response_text: str, turn_num: int, turn_total: int):
        """Send a turn relay to the other agent via lk.agent.chat."""
        try:
            relay = f"[TURN_RELAY:{turn_num}/{turn_total}] {response_text}"
            await ctx.room.local_participant.send_text(
                relay, topic="lk.agent.chat",
                attributes={"lk.chat.sender_name": agent_name},
            )
            logger.info(
                f"[{agent_name}] Turn relay sent ({turn_num}/{turn_total}): "
                f"{response_text[:50]}"
            )
        except Exception:
            logger.warning(f"[{agent_name}] Failed to send turn relay", exc_info=True)

    async def _respond_and_maybe_relay(text: str, sender: str):
        """Get a response, speak it, and relay to other agent if turns remain."""
        await session.interrupt()
        await _broadcast_thinking()
        response, preserve_error, already_spoken = await _get_spoken_response(text, sender)
        await _say_and_echo(response, preserve_error=preserve_error, already_spoken=already_spoken)

        # If turns remain, relay our response to trigger the other agent
        remaining = _turn_remaining[0]
        if remaining > 0:
            _turn_remaining[0] = remaining - 1
            total = _turn_total[0]
            turn_num = total - _turn_remaining[0]
            await _send_turn_relay(response, turn_num, total)

    async def _handle_human_input(text: str, sender: str):
        """Process human input through turn-taking → OpenClaw → TTS."""
        # Fix STT misrecognitions of agent names before any name checks
        text = _correct_names(text)

        # Global dedup: skip if we already processed this exact text recently
        # (catches voice→chat echo duplication even after name correction)
        dedup_key = f"human:{text.strip().lower()}"
        now = time.monotonic()
        prev = _processed_texts.get(dedup_key)
        if prev and now - prev < _TEXT_DEDUP_WINDOW:
            logger.debug(f"[{agent_name}] Skipping duplicate human input: {text[:40]}")
            return
        _processed_texts[dedup_key] = now
        # Prune old dedup entries
        for k in [k for k, v in _processed_texts.items() if now - v > _TEXT_DEDUP_WINDOW * 3]:
            del _processed_texts[k]

        # Stop command: cancel any active turn-taking
        if is_stop_command(text):
            if _turn_remaining[0] > 0:
                _turn_remaining[0] = 0
                _turn_total[0] = 0
                logger.info(f"[{agent_name}] Turn-taking stopped by {sender}")
            await _broadcast_idle()
            return

        # Check for turn count request ("talk for 5 turns")
        requested_turns = parse_turn_count(text)

        # Check for group address ("hey guys", "you two", "both of you")
        group = is_group_address(text)

        # If turns requested, primary starts the conversation
        if requested_turns > 0:
            if _is_primary(agent_name):
                _turn_total[0] = requested_turns
                _turn_remaining[0] = requested_turns - 1  # we take the first turn
                _turn_initiator[0] = sender
                logger.info(
                    f"[{agent_name}] Starting {requested_turns}-turn conversation "
                    f"(requested by {sender})"
                )
                await _respond_and_maybe_relay(text, sender)
            else:
                # Non-primary waits — primary will relay to us
                logger.debug(f"[{agent_name}] Turn-taking: waiting for primary to start")
            return

        # Skip if human addresses a different agent (not us)
        if _human_mentions_other(text) and not group:
            logger.debug(f"[{agent_name}] Skipping — human addressed other agent")
            await _broadcast_idle()
            return

        # If human mentions ONLY us (not both agents, not group), respond immediately
        if _human_mentions_us(text) and not _human_mentions_both(text) and not group:
            await _respond_and_maybe_relay(text, sender)
            return

        # Group address or both agents mentioned: both respond sequentially
        # Default to 4 total turns (2 each) for group address
        if group or _human_mentions_both(text):
            if _is_primary(agent_name):
                total = 4  # 2 turns each
                _turn_total[0] = total
                _turn_remaining[0] = total - 1
                _turn_initiator[0] = sender
                logger.info(f"[{agent_name}] Group address: starting {total}-turn exchange")
                await _respond_and_maybe_relay(text, sender)
            else:
                # Non-primary waits for relay from primary
                logger.debug(f"[{agent_name}] Group address: waiting for primary")
            return

        # Generic message (no names, no group): staggered delay, only one responds
        delay = _get_delay(agent_name)
        await asyncio.sleep(delay)
        if _is_other_agent_busy():
            logger.debug(f"[{agent_name}] Skipping — other agent already responding")
            await _broadcast_idle()
            return

        # Claim the turn by broadcasting "thinking" IMMEDIATELY
        await _broadcast_thinking()
        # Small grace period for the other agent to see our claim
        await asyncio.sleep(0.5)
        # Double-check: if other agent also claimed, non-primary yields
        if _is_other_agent_busy() and not _is_primary(agent_name):
            logger.debug(f"[{agent_name}] Skipping — other agent also claimed, yielding")
            await _broadcast_idle()
            return

        await _respond_and_maybe_relay(text, sender)

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
            status="Reading chat input",
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

        # Check for voice relay from the primary agent.
        # Format: [VOICE_RELAY:sender_name] actual text
        # Non-primary agents process this as human voice input.
        relay_match = _VOICE_RELAY_RE.match(text)
        if relay_match and not _is_primary(agent_name):
            original_sender = relay_match.group(1)
            actual_text = relay_match.group(2).strip()
            if actual_text:
                logger.info(
                    f"[{agent_name}] Voice relay: '{actual_text[:60]}' "
                    f"(sender: {original_sender})"
                )
                await _handle_human_input(actual_text, original_sender)
            return

        # Check for turn relay: automatic turn-taking between agents.
        # Format: [TURN_RELAY:turn_num/total] previous agent's response
        turn_match = _TURN_RELAY_RE.match(text)
        if turn_match:
            turn_num = int(turn_match.group(1))
            turn_total = int(turn_match.group(2))
            prev_response = turn_match.group(3).strip()
            remaining = turn_total - turn_num
            if remaining <= 0 or not prev_response:
                logger.info(f"[{agent_name}] Turn relay: no turns left, ignoring")
                return

            # Get the sender agent's display name
            p = ctx.room.remote_participants.get(sender_identity)
            from_name = _sanitize_name(
                p.name or p.attributes.get("agent_name") or sender_identity
            ) if p else sender_identity

            logger.info(
                f"[{agent_name}] Turn relay ({turn_num}/{turn_total}): "
                f"responding to {from_name}"
            )

            # Update our turn state so _respond_and_maybe_relay continues the chain
            _turn_total[0] = turn_total
            _turn_remaining[0] = remaining - 1  # we'll use one turn now
            sender = _turn_initiator[0] or _get_human_name()

            # Build context from previous agent's response
            prompt = (
                f"[{from_name} just said]: {prev_response}\n\n"
                f"Continue the conversation naturally. "
                f"Respond to what {from_name} said. "
                f"Turn {turn_num + 1} of {turn_total}."
            )

            await _wait_for_other_agent_idle(timeout=15.0)
            await asyncio.sleep(0.5)
            await _respond_and_maybe_relay(prompt, sender)
            return

        # Non-relay agent message: store for context only (no response triggered).
        # Agent-to-agent conversation uses TURN_RELAY, not mention cascades.
        sender_name = sender_identity
        p = ctx.room.remote_participants.get(sender_identity)
        if p:
            sender_name = _sanitize_name(p.name or p.attributes.get("agent_name") or p.identity)
        if p and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
            return

        logger.debug(f"[{agent_name}] Agent chat from {sender_name}: noted for context")
        _store_context_message(sender_name, text)

    def _handle_agent_chat(reader: rtc.TextStreamReader, sender_identity: str):
        asyncio.create_task(_handle_agent_chat_async(reader, sender_identity))

    # --- Cleanup ---
    async def _cleanup():
        logger.info(f"Agent '{agent_name}' shutting down in room '{room_name}'")
        try:
            await _bus.close()
        except Exception:
            pass
        if _ACP_ENABLED:
            try:
                await _acp_close()
            except Exception:
                pass
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

        # --- Audio track filtering (BUG 1 + BUG 3) ---
        # Primary agent: unsubscribe from AGENT audio tracks (prevent TTS→STT loop)
        # Non-primary agent: unsubscribe from ALL audio (voice arrives via relay)
        _primary_flag = _is_primary(agent_name)

        def _should_unsub_audio(participant: rtc.RemoteParticipant) -> bool:
            if not _primary_flag:
                return True  # Non-primary: no audio at all
            return participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT

        for _rp in ctx.room.remote_participants.values():
            if _should_unsub_audio(_rp):
                for _pub in _rp.track_publications.values():
                    if _pub.kind == rtc.TrackKind.KIND_AUDIO:
                        _pub.set_subscribed(False)
                        logger.debug(f"[{agent_name}] Unsubscribed audio: {_rp.identity}")

        @ctx.room.on("track_published")
        def _on_track_published(
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            if publication.kind == rtc.TrackKind.KIND_AUDIO and _should_unsub_audio(participant):
                publication.set_subscribed(False)
                logger.debug(f"[{agent_name}] Unsubscribed new audio: {participant.identity}")

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

            # Mark BOTH original and name-corrected text as processed
            # so text_input_cb doesn't double-handle after echo
            _voice_processed[text.lower()] = time.monotonic()
            corrected = _correct_names(text)
            if corrected.lower() != text.lower():
                _voice_processed[corrected.lower()] = time.monotonic()
            # Prune old entries
            now = time.monotonic()
            for k in [k for k, v in _voice_processed.items() if now - v > _VOICE_DEDUP_WINDOW]:
                del _voice_processed[k]

            asyncio.create_task(_process_voice_input(corrected, sender))

        async def _process_voice_input(text: str, sender: str):
            """Echo voice transcription to chat (so it's visible) and process it.

            Only the primary agent runs STT and handles voice directly.
            Non-primary agents receive voice via relay on lk.agent.chat.
            """
            # text is already name-corrected by _on_user_input_transcribed

            # Non-primary agents should never reach here (audio unsubscribed),
            # but guard anyway.
            if not _is_primary(agent_name):
                return

            # Echo the transcription to lk.chat so it shows in the UI
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

            # Relay voice transcription to non-primary agents via agent chat
            try:
                relay_msg = f"[VOICE_RELAY:{sender}] {text}"
                await ctx.room.local_participant.send_text(
                    relay_msg, topic="lk.agent.chat",
                    attributes={"lk.chat.sender_name": agent_name},
                )
                logger.debug(f"[{agent_name}] Relayed voice to agent chat: {text[:60]}")
            except Exception:
                logger.warning(f"[{agent_name}] Failed to relay voice to agent chat", exc_info=True)

            # Process through the standard human input handler
            await _handle_human_input(text, sender)

        # Static greeting — zero LLM calls
        greeting_delay = _get_delay(agent_name) + 0.5
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
