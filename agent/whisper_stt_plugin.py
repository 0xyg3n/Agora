"""Custom LiveKit STT plugin wrapping faster-whisper (local, no API key)."""

import asyncio
import logging
import uuid

import numpy as np
from faster_whisper import WhisperModel
from livekit.agents import stt, utils

logger = logging.getLogger("whisper-stt")


class WhisperSTT(stt.STT):
    """LiveKit STT adapter for faster-whisper (local inference)."""

    def __init__(
        self,
        *,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            ),
        )
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model: WhisperModel | None = None

    def _ensure_model(self) -> WhisperModel:
        if self._model is None:
            logger.info(
                f"Loading whisper model '{self._model_size}' "
                f"(device={self._device}, compute={self._compute_type})"
            )
            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )
        return self._model

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language=None,
        conn_options=None,
    ) -> stt.SpeechEvent:
        # Merge frames into a single audio buffer
        if isinstance(buffer, list):
            frame = utils.merge_frames(buffer)
        else:
            frame = buffer

        # Convert to float32 numpy array normalized to [-1, 1]
        audio_data = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0

        # Resample to 16kHz if needed (whisper expects 16kHz)
        if frame.sample_rate != 16000:
            duration = len(audio_data) / frame.sample_rate
            target_len = int(duration * 16000)
            indices = np.linspace(0, len(audio_data) - 1, target_len)
            audio_data = np.interp(indices, np.arange(len(audio_data)), audio_data)

        # Ensure float32 — ONNX runtime requires it, np.interp returns float64
        audio_data = audio_data.astype(np.float32)

        # Run whisper in a thread to avoid blocking the event loop
        model = self._ensure_model()
        lang = language or self._language

        loop = asyncio.get_event_loop()
        segments, info = await loop.run_in_executor(
            None,
            lambda: model.transcribe(
                audio_data,
                language=lang,
                beam_size=3,
                vad_filter=False,
            ),
        )

        # Collect all segments
        text_parts = []
        for segment in segments:
            text_parts.append(segment.text.strip())

        text = " ".join(text_parts).strip()
        detected_lang = info.language if info else "en"

        logger.debug(f"Transcribed ({detected_lang}): {text}")

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=str(uuid.uuid4()),
            alternatives=[
                stt.SpeechData(
                    language=detected_lang,
                    text=text,
                    confidence=info.language_probability if info else 0.0,
                )
            ],
        )
