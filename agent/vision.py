"""Vision module for Agora — frame capture + LLM vision API.

Handles:
  1. Intent detection (is the user asking about what they see?)
  2. Single-frame capture from camera or screen share
  3. Direct vision API call — Claude for Laira, OpenAI for Loki (bypasses OpenClaw)
"""

import asyncio
import base64
import io
import logging
import os
import re

import anthropic
import httpx
import openai
from livekit import rtc
from PIL import Image

logger = logging.getLogger("agora-vision")

# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

_VISION_PATTERNS = [
    r"\bcan you (see|view)\b",
    r"\bdo you see\b",
    r"\bwhat do you see\b",
    r"\blook at (me|this|my)\b",
    r"\b(see|view) my\b",
    r"\bwhat am i (wearing|doing|holding|showing)\b",
    r"\bdescribe.{0,30}(see|view)\b",
    r"\bhow do i look\b",
    r"\bwhat.{0,20}(see|view).{0,20}screen\b",
    r"\bwhat'?s on my screen\b",
    r"\bread my screen\b",
    r"\bcheck my (screen|camera|share\s*screen)\b",
    r"\b(see|view).{0,15}(screen|camera|sharing)\b",
    r"\bwhat.{0,10}(screen|camera)\b",
]
_VISION_RE = re.compile("|".join(_VISION_PATTERNS), re.IGNORECASE)


def is_vision_request(text: str) -> bool:
    """Check if text is asking about what the agent can see."""
    return bool(_VISION_RE.search(text))


# ---------------------------------------------------------------------------
# Frame capture
# ---------------------------------------------------------------------------

def _find_human_participant(room: rtc.Room) -> rtc.RemoteParticipant | None:
    for p in room.remote_participants.values():
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
            return p
    return None


def _encode_frame_jpeg(frame) -> bytes:
    """Encode a VideoFrame to JPEG bytes (max 512×512)."""
    # Try livekit's built-in encoder first (handles format conversion natively)
    try:
        from livekit.agents.utils.images import encode, EncodeOptions, ResizeOptions

        return encode(
            frame,
            EncodeOptions(
                format="JPEG",
                resize_options=ResizeOptions(
                    width=512, height=512, strategy="scale_aspect_fit"
                ),
            ),
        )
    except Exception:
        pass

    # Fallback: assume RGBA raw data → Pillow
    img = Image.frombytes("RGBA", (frame.width, frame.height), bytes(frame.data))
    img = img.convert("RGB")
    img.thumbnail((512, 512), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


async def capture_frame(room: rtc.Room, source: str = "camera") -> bytes | None:
    """Capture a single JPEG frame from the human's camera or screen share."""
    participant = _find_human_participant(room)
    if not participant:
        logger.warning("Vision: no human participant found")
        return None

    # Determine track sources to try
    if source == "screen":
        sources = [rtc.TrackSource.SOURCE_SCREENSHARE, rtc.TrackSource.SOURCE_CAMERA]
    else:
        sources = [rtc.TrackSource.SOURCE_CAMERA, rtc.TrackSource.SOURCE_SCREENSHARE]

    for track_source in sources:
        stream = None
        frame = None
        try:
            stream = rtc.VideoStream.from_participant(
                participant=participant,
                track_source=track_source,
                format=rtc.VideoBufferType.RGBA,
                capacity=1,
            )

            async def _grab():
                async for event in stream:
                    return event.frame
                return None

            frame = await asyncio.wait_for(_grab(), timeout=8.0)
        except asyncio.TimeoutError:
            logger.debug(f"Vision: no frame from source {track_source} (8s timeout)")
        except Exception:
            logger.debug(f"Vision: failed to capture from source {track_source}", exc_info=True)
        finally:
            if stream:
                try:
                    await asyncio.wait_for(stream.aclose(), timeout=2.0)
                except Exception:
                    pass

        if frame is not None:
            logger.info(f"Vision: captured frame from {track_source} ({frame.width}x{frame.height})")
            jpeg = _encode_frame_jpeg(frame)
            logger.info(f"Vision: encoded JPEG ({len(jpeg)} bytes)")
            return jpeg

    logger.warning(f"Vision: no video frames available (tried {sources})")
    return None


# ---------------------------------------------------------------------------
# Vision API — Laira uses Claude, Loki uses OpenAI
# ---------------------------------------------------------------------------

# Agent name → provider mapping
# Vision API always uses Claude (flat-rate OAuth subscription).
# Loki's normal messages still go through OpenClaw → GPT-5.4.
_AGENT_PROVIDERS = {"laira": "claude", "loki": "claude"}

# --- Claude client (Laira) ---
_OAUTH_BETA = (
    "interleaved-thinking-2025-05-14,"
    "fine-grained-tool-streaming-2025-05-14,"
    "claude-code-20250219,"
    "oauth-2025-04-20"
)
_OAUTH_UA = "claude-cli/2.1.92 (external, cli)"
_claude_client: anthropic.AsyncAnthropic | None = None


def _get_claude_client() -> anthropic.AsyncAnthropic:
    global _claude_client
    if _claude_client is not None:
        return _claude_client

    token = (
        os.environ.get("ANTHROPIC_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if not token:
        raise ValueError("No Anthropic credentials for vision")

    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    is_oauth = "sk-ant-oat" in token

    http = httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )

    if is_oauth:
        _claude_client = anthropic.AsyncAnthropic(
            api_key=None,
            auth_token=token,
            base_url=base_url,
            default_headers={
                "anthropic-beta": _OAUTH_BETA,
                "user-agent": _OAUTH_UA,
                "x-app": "cli",
            },
            http_client=http,
        )
    else:
        _claude_client = anthropic.AsyncAnthropic(
            api_key=token,
            base_url=base_url,
            http_client=http,
        )
    return _claude_client


# --- OpenAI client (Loki) ---
_openai_client: openai.AsyncOpenAI | None = None


def _get_openai_client() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    token = (
        os.environ.get("OPENAI_CODEX_TOKEN")
        or os.environ.get("OPENAI_API_KEY")
    )
    if not token:
        raise ValueError("No OpenAI credentials for vision")

    _openai_client = openai.AsyncOpenAI(
        api_key=token,
        timeout=30.0,
    )
    return _openai_client


# --- Unified vision entry point ---

_VISION_SYSTEM = (
    "You are {agent_name}, an AI agent in a live voice room with a human. "
    "You can see them through their webcam right now. "
    "Answer their question about what you see accurately and specifically. "
    "Be observant: note details like clothing, gestures, fingers held up, "
    "objects, expressions, and background. "
    "Reply in 1-2 natural sentences, max 30 words. No markdown."
)


async def ask_claude_vision(image_bytes: bytes, user_text: str, agent_name: str) -> str:
    """Send image + user question to the appropriate vision API."""
    provider = _AGENT_PROVIDERS.get(agent_name.lower(), "claude")
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    system = _VISION_SYSTEM.format(agent_name=agent_name)

    try:
        if provider == "openai":
            return await _ask_openai_vision(b64, user_text, system, agent_name)
        else:
            return await _ask_claude_vision(b64, user_text, system, agent_name)
    except asyncio.TimeoutError:
        logger.error(f"Vision: {provider} API call timed out (15s)")
        return "Sorry, the vision request timed out. Try again?"
    except Exception:
        logger.exception(f"Vision: {provider} API call failed")
        return "Sorry, I had trouble seeing that. Could you try again?"


_VISION_MODEL = os.environ.get("ANTHROPIC_VISION_MODEL", "claude-sonnet-4-6")
_VISION_FALLBACK_MODEL = "claude-haiku-4-5-20251001"


async def _call_vision_model(client, model: str, b64: str, user_text: str, system: str) -> str:
    """Single vision API call to a specific model."""
    resp = await asyncio.wait_for(
        client.messages.create(
            model=model,
            max_tokens=150,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": user_text},
                ],
            }],
        ),
        timeout=15.0,
    )
    return resp.content[0].text.strip()


async def _ask_claude_vision(b64: str, user_text: str, system: str, agent_name: str) -> str:
    client = _get_claude_client()

    # Try primary model first
    try:
        logger.info(f"Vision: calling Claude API (model={_VISION_MODEL}, agent={agent_name})")
        text = await _call_vision_model(client, _VISION_MODEL, b64, user_text, system)
        logger.info(f"Vision: Claude response: {text[:80]}...")
        return text
    except anthropic.RateLimitError:
        logger.warning(f"Vision: {_VISION_MODEL} rate limited")

    # Fallback to Haiku if primary is rate limited
    if _VISION_MODEL != _VISION_FALLBACK_MODEL:
        try:
            logger.info(f"Vision: falling back to {_VISION_FALLBACK_MODEL}")
            text = await _call_vision_model(client, _VISION_FALLBACK_MODEL, b64, user_text, system)
            logger.info(f"Vision: fallback response: {text[:80]}...")
            return text
        except anthropic.RateLimitError:
            logger.error(f"Vision: fallback model also rate limited")

    raise anthropic.RateLimitError(
        message="All vision models rate limited",
        response=None, body=None,
    )


async def _ask_openai_vision(b64: str, user_text: str, system: str, agent_name: str) -> str:
    client = _get_openai_client()
    logger.info(f"Vision: calling OpenAI API (agent={agent_name})")
    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            max_tokens=150,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": user_text},
                ]},
            ],
        ),
        timeout=15.0,
    )
    text = resp.choices[0].message.content.strip()
    logger.info(f"Vision: OpenAI response: {text[:80]}...")
    return text
