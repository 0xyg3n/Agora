"""Custom LiveKit TTS plugin wrapping edge-tts (livekit-agents 1.4.x).

Streams decoded PCM chunks as they arrive from edge-tts instead of
buffering the entire response, reducing time-to-first-byte significantly.
"""

import io
import uuid

import av
import edge_tts
from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS


class EdgeTTS(tts.TTS):
    def __init__(self, *, voice: str = "en-US-AriaNeural", rate: str = "+0%", volume: str = "+0%"):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._voice = voice
        self._rate = rate
        self._volume = volume

    def synthesize(self, text: str, *, conn_options=DEFAULT_API_CONNECT_OPTIONS) -> "EdgeChunkedStream":
        return EdgeChunkedStream(
            tts=self, input_text=text,
            voice=self._voice, rate=self._rate, volume=self._volume,
            conn_options=conn_options,
        )


# Minimum mp3 bytes to accumulate before attempting a decode pass.
# edge-tts sends small chunks (~1-4 KB); we batch to avoid decoding
# partial mp3 frames while still keeping latency low.
_CHUNK_THRESHOLD = 8 * 1024  # 8 KB ≈ ~200ms of audio at 128kbps


class EdgeChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts, input_text, voice, rate, volume, conn_options):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._voice = voice
        self._rate = rate
        self._volume = volume

    async def _run(self, output: tts.AudioEmitter):
        request_id = str(uuid.uuid4())
        output.initialize(
            request_id=request_id,
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
        )

        communicate = edge_tts.Communicate(
            self._input_text,
            voice=self._voice,
            rate=self._rate,
            volume=self._volume,
        )

        mp3_buffer = bytearray()

        async for chunk in communicate.stream():
            if chunk["type"] != "audio":
                continue
            mp3_buffer.extend(chunk["data"])

            # Decode and push a batch once we have enough data
            if len(mp3_buffer) >= _CHUNK_THRESHOLD:
                pcm = _decode_mp3_to_pcm(bytes(mp3_buffer))
                if pcm:
                    output.push(pcm)
                mp3_buffer.clear()

        # Flush remaining audio
        if mp3_buffer:
            pcm = _decode_mp3_to_pcm(bytes(mp3_buffer))
            if pcm:
                output.push(pcm)


def _decode_mp3_to_pcm(mp3_bytes: bytes) -> bytes | None:
    """Decode mp3 bytes to 24kHz mono s16le PCM."""
    buf = io.BytesIO(mp3_bytes)
    try:
        container = av.open(buf, format="mp3")
    except av.error.InvalidDataError:
        return None

    resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)
    pcm_data = bytearray()

    for frame in container.decode(audio=0):
        for r in resampler.resample(frame):
            pcm_data.extend(bytes(r.planes[0]))

    container.close()
    return bytes(pcm_data) if pcm_data else None
