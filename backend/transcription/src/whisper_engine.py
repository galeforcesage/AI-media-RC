"""
whisper_engine.py
Whisper transcription engine using faster-whisper (CTranslate2).

Supports automatic model selection based on available RAM.
Outputs timestamped segments.
"""

from __future__ import annotations
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .gpu import release_cuda_memory

logger = logging.getLogger(__name__)


def _get_available_ram_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb / (1024 * 1024)
    except Exception:
        return 8.0  # safe default
    return 8.0


def select_model(preferred: str = "large-v3") -> str:
    """Select Whisper model based on available RAM."""
    ram = _get_available_ram_gb()
    logger.info("Available RAM: %.1f GB, preferred model: %s", ram, preferred)

    if preferred == "large-v3" and ram >= 10:
        return "large-v3"
    elif ram >= 6:
        return "medium"
    elif ram >= 3:
        return "small"
    else:
        return "base"


# Reserve CPU headroom for SageTV, Channels DVR, and other services.
# Use 25% of available cores (minimum 1) so transcription stays low-priority.
import os as _os
DEFAULT_CPU_THREADS = max(1, _os.cpu_count() // 4) if _os.cpu_count() else 2


def _detect_device() -> str:
    """Detect GPU availability. Returns 'cuda' if usable, otherwise 'cpu'."""
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            vram_mb = torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
            logger.info("GPU detected: %s (%.0f MB VRAM) — using CUDA", gpu_name, vram_mb)
            return "cuda"
    except Exception:
        logger.exception("GPU detection failed; falling back to CPU")
        return "cpu"
    logger.info("No GPU detected — using CPU")
    return "cpu"


class WhisperEngine:
    """Async-friendly wrapper around faster-whisper."""

    def __init__(
        self,
        model_name: str = "auto",
        device: str = "auto",
        compute_type: str = "auto",
        cpu_threads: int = DEFAULT_CPU_THREADS,
    ):
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._model = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def load(self) -> None:
        """Load the Whisper model (call once at startup)."""
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.error("faster-whisper not installed. Install with: pip install faster-whisper")
            raise

        if self._model_name == "auto":
            self._model_name = select_model()

        # Auto-detect device: use CUDA if available, otherwise CPU
        if self._device == "auto":
            self._device = _detect_device()
        # Auto-detect compute type based on device
        if self._compute_type == "auto":
            self._compute_type = "float16" if self._device == "cuda" else "int8"

        logger.info("Loading Whisper model: %s (device=%s, compute=%s, threads=%d)",
                     self._model_name, self._device, self._compute_type, self._cpu_threads)
        start = time.time()
        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
        )
        logger.info("Whisper model loaded in %.1fs", time.time() - start)

    def unload(self) -> None:
        """Release the model and hand its VRAM back to the driver.

        Safe to call at any time — ``transcribe()`` reloads on demand.
        """
        if self._model is None:
            return
        logger.info("Unloading Whisper model %s (device=%s) to free VRAM",
                    self._model_name, self._device)
        self._model = None
        release_cuda_memory()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def uses_gpu(self) -> bool:
        """Whether this engine will run on the GPU.

        Before the first load ``_device`` may still be "auto", so treat anything
        that isn't an explicit "cpu" as GPU-bound; callers use this only to
        decide whether a VRAM headroom check is worth doing.
        """
        return self._device != "cpu"

    def transcribe(self, audio_path: str, language: str = "en",
                   should_stop=None) -> Tuple[str, List[Dict], Dict]:
        """Transcribe an audio file.

        Args:
            should_stop: optional zero-arg callable. Checked at every segment
                boundary; when it returns True the loop stops early so the GPU
                can be handed to a higher-priority tenant. The returned info
                then carries ``interrupted=True`` and ``resume_at`` (seconds of
                audio already transcribed) so a caller can continue later.

        Returns:
            (full_text, segments, info)
            segments: list of {start, end, text}
            info: {language, duration, language_probability, interrupted, resume_at}
        """
        if self._model is None:
            self.load()

        logger.info("Transcribing: %s", audio_path)
        start = time.time()

        segments_iter, info = self._model.transcribe(
            audio_path,
            language=language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )

        full_text_parts = []
        segments = []
        interrupted = False
        resume_at = 0.0
        for seg in segments_iter:
            if should_stop is not None and should_stop():
                interrupted = True
                logger.info("Transcription interrupted at %.1fs (GPU yield)", resume_at)
                break
            full_text_parts.append(seg.text.strip())
            segments.append({
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
            })
            resume_at = seg.end

        elapsed = time.time() - start
        full_text = " ".join(full_text_parts)
        transcription_info = {
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration": round(info.duration, 2),
            "model": self._model_name,
            "elapsed_seconds": round(elapsed, 1),
            "realtime_factor": round(elapsed / max(info.duration, 1), 2),
            "interrupted": interrupted,
            "resume_at": round(resume_at, 2),
        }

        logger.info("Transcription %s: %.0fs audio in %.0fs (%.2fx realtime), %d segments",
                     "interrupted" if interrupted else "complete",
                     info.duration, elapsed, elapsed / max(info.duration, 1), len(segments))

        return full_text, segments, transcription_info

    def transcribe_clip(self, audio_path: str, time_offset: float, language: str = "en") -> List[Dict]:
        """Transcribe a sliced clip and shift timestamps by `time_offset`.

        Returns a list of {start, end, text} segments in absolute (file-wide)
        time. Used by the gap-fill STT path.
        """
        if self._model is None:
            self.load()
        if not os.path.exists(audio_path):
            return []
        try:
            segments_iter, _info = self._model.transcribe(
                audio_path,
                language=language,
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
        except Exception:
            logger.exception("Gap-fill transcribe failed for %s", audio_path)
            return []
        out: List[Dict] = []
        for seg in segments_iter:
            text = seg.text.strip()
            if not text:
                continue
            out.append({
                "start": round(seg.start + time_offset, 2),
                "end": round(seg.end + time_offset, 2),
                "text": text,
            })
        return out

    def segments_to_vtt(self, segments: List[Dict]) -> str:
        """Convert segments to WebVTT subtitle format with optional speaker labels."""
        lines = ["WEBVTT", ""]
        prev_speaker = None
        for i, seg in enumerate(segments):
            start = self._format_time(seg["start"])
            end = self._format_time(seg["end"])
            lines.append(str(i + 1))
            lines.append(f"{start} --> {end}")
            speaker = seg.get("speaker")
            text = seg["text"]
            if speaker:
                if speaker != prev_speaker:
                    text = f"<v {speaker}>{text}"
                    prev_speaker = speaker
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _format_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"
