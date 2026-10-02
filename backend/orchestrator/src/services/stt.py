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

from services.gpu import release_cuda_memory

logger = logging.getLogger(__name__)

# Release the STT model after this many seconds without a transcription.
# Voice commands are bursty and short, so holding a CUDA context between
# utterances wastes VRAM that Whisper transcription / Ollama could use.
DEFAULT_IDLE_TIMEOUT = 300.0
_REAPER_INTERVAL = 30.0

# Which device to run voice-command STT on: "cpu" (default), "cuda", or "auto".
#
# Defaults to CPU deliberately. Unloading the model frees its weights but NOT
# the CUDA primary context, which costs ~316 MiB and can only be reclaimed by
# exiting the process -- so a single voice command pins that memory for the
# lifetime of the orchestrator. This GPU is shared with a live TV transcoder,
# batch transcription (Whisper large-v3) and Ollama, and measured usage here is
# 2 utterances in 3 weeks, so that trade is a bad one. The "base" model at int8
# on CPU handles short utterances comfortably.
#
# Set STT_DEVICE=auto (or cuda) if voice latency ever becomes the bottleneck.
DEFAULT_DEVICE = os.environ.get("STT_DEVICE", "cpu").strip().lower()
_CPU_THREADS = int(os.environ.get("STT_CPU_THREADS", "4"))


class STTService:
    """Voice command transcription using faster-whisper.

    The model is loaded lazily on first use and released again once the
    service has been idle for ``idle_timeout`` seconds.
    """

    def __init__(
        self,
        model_name: str = "base",
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        device: str = DEFAULT_DEVICE,
    ):
        self._model_name = model_name
        self._model = None
        self._lock = asyncio.Lock()
        self._device = "cpu"
        self._requested_device = device
        self._idle_timeout = idle_timeout
        self._last_used = 0.0
        self._reaper: Optional[asyncio.Task] = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        self._device = "cpu"
        compute_type = "int8"
        # Only touch torch.cuda when CUDA was actually asked for; probing it
        # unconditionally would create the CUDA context we are trying to avoid.
        if self._requested_device in ("cuda", "auto"):
            try:
                import torch
                if torch.cuda.is_available():
                    self._device = "cuda"
                    compute_type = "float16"
                elif self._requested_device == "cuda":
                    logger.warning("STT_DEVICE=cuda requested but CUDA is unavailable; using CPU")
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
            cpu_threads=_CPU_THREADS if self._device == "cpu" else 2,
        )
        logger.info("Whisper STT model loaded in %.1fs", time.time() - start)

    def unload(self) -> None:
        """Drop the model and hand its VRAM back to the driver."""
        if self._model is None:
            return
        logger.info("Releasing idle Whisper STT model (device=%s)", self._device)
        self._model = None
        if self._device == "cuda":
            release_cuda_memory()

    def _touch(self) -> None:
        self._last_used = time.time()

    def _ensure_reaper(self) -> None:
        """Start the idle-unload task (lazily, once an event loop exists)."""
        if self._idle_timeout <= 0:
            return
        if self._reaper is not None and not self._reaper.done():
            return
        try:
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())
        except RuntimeError:
            pass  # no running loop yet; will retry on next transcribe

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(_REAPER_INTERVAL)
            if self._model is None:
                continue
            if time.time() - self._last_used < self._idle_timeout:
                continue
            async with self._lock:
                # Re-check under the lock so we never unload mid-transcription
                if self._model is not None and time.time() - self._last_used >= self._idle_timeout:
                    await asyncio.get_event_loop().run_in_executor(None, self.unload)

    async def aclose(self) -> None:
        """Cancel the reaper and release the model (call on shutdown)."""
        if self._reaper:
            self._reaper.cancel()
            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper = None
        self.unload()

    async def transcribe_pcm(
        self, pcm_bytes: bytes, sample_rate: int = 16000
    ) -> str:
        """Transcribe raw PCM16-LE mono audio bytes."""
        self._ensure_reaper()
        async with self._lock:
            self._touch()
            try:
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._transcribe_sync, pcm_bytes, sample_rate,
                )
            finally:
                self._touch()

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
