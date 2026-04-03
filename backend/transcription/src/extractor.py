"""
extractor.py
Audio extraction worker.

Extracts audio from recordings to SSD temp storage using ffmpeg.
Avoids HDD contention per Appendix D.
Supports partial extraction for incremental/live transcription.
"""

from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class AudioExtractor:
    """Extracts audio from video files using ffmpeg."""

    def __init__(self, ssd_temp_dir: str = "/tmp/transcription"):
        self.ssd_temp_dir = ssd_temp_dir
        Path(ssd_temp_dir).mkdir(parents=True, exist_ok=True)

    async def extract(
        self,
        video_path: str,
        recording_id: str,
        start_seconds: Optional[float] = None,
    ) -> str:
        """Extract audio to WAV on SSD. Returns path to temp audio file.

        If start_seconds is given, extract only from that point onward
        (used for incremental/live transcription).
        """
        suffix = ""
        if start_seconds is not None and start_seconds > 0:
            suffix = f"_from{int(start_seconds)}"
        output_path = os.path.join(self.ssd_temp_dir, f"{recording_id}{suffix}.wav")

        if os.path.exists(output_path):
            logger.debug("Audio already extracted: %s", output_path)
            return output_path

        cmd = ["ffmpeg"]
        if start_seconds is not None and start_seconds > 0:
            cmd += ["-ss", str(start_seconds)]
        cmd += [
            "-i", video_path,
            "-vn",  # no video
            "-acodec", "pcm_s16le",  # 16-bit PCM
            "-ar", "16000",  # 16kHz for Whisper
            "-ac", "1",  # mono
            "-y",  # overwrite
            output_path,
        ]

        logger.info("Extracting audio: %s -> %s (start=%.1fs)",
                     video_path, output_path, start_seconds or 0)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[-500:]
            raise RuntimeError(f"ffmpeg extraction failed (rc={proc.returncode}): {err}")

        logger.info("Audio extracted: %s (%.1f MB)", output_path,
                     os.path.getsize(output_path) / (1024 * 1024))
        return output_path

    async def get_duration(self, audio_path: str) -> float:
        """Get duration of an audio/video file in seconds using ffprobe."""
        cmd = [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        try:
            return float(stdout.decode().strip())
        except (ValueError, AttributeError):
            return 0.0

    def cleanup(self, audio_path: str) -> None:
        """Remove temp audio file after transcription."""
        try:
            if os.path.exists(audio_path):
                os.remove(audio_path)
                logger.debug("Cleaned up temp audio: %s", audio_path)
        except OSError as e:
            logger.warning("Failed to clean up %s: %s", audio_path, e)
