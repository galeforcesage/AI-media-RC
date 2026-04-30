"""
stt.py — Real-time speech-to-text using faster-whisper.

Lightweight Whisper model for transcribing short voice commands.
Uses the "base" model (~150MB VRAM) which is fast enough for
real-time partials on GPU.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
import wave
from typing import Optional

logger = logging.getLogger(__name__)


class STTService:
    """Voice command transcription using faster-whisper."""

    def __init__(self, model_name: str = "base"):
        self._model_name = model_name
        self._model = None
        self._lock = asyncio.Lock()
        self._device = "cpu"

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        # Detect GPU
        self._device = "cpu"
        compute_type = "int8"
        try:
            import torch
            if torch.cuda.is_available():
                self._device = "cuda"
                compute_type = "float16"
        except ImportError:
            pass

        logger.info(
            "Loading Whisper STT model: %s (device=%s, compute=%s)",
            self._model_name, self._device, compute_type,
        )
        start = time.time()
        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=compute_type,
            cpu_threads=2,
        )
        logger.info("Whisper STT model loaded in %.1fs", time.time() - start)

    async def transcribe_pcm(
        self, pcm_bytes: bytes, sample_rate: int = 16000
    ) -> str:
        """Transcribe raw PCM16-LE mono audio bytes."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._transcribe_sync, pcm_bytes, sample_rate,
            )

    def _transcribe_sync(self, pcm_bytes: bytes, sample_rate: int) -> str:
        self._ensure_loaded()
        if len(pcm_bytes) < 3200:  # <0.1s of audio
            return ""

        # Write PCM to a temporary WAV file for faster-whisper
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False
            ) as f:
                tmp_path = f.name
                with wave.open(f, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(sample_rate)
                    wf.writeframes(pcm_bytes)

            segments, _info = self._model.transcribe(
                tmp_path,
                language="en",
                beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            text = " ".join(s.text.strip() for s in segments)
            return text.strip()
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
