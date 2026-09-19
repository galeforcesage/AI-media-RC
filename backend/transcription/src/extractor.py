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
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ffmpeg messages that mean "retrying this will fail exactly the same way".
# Everything else (I/O hiccups, a file still being written, transient disk
# pressure) is worth another attempt.
_PERMANENT_FFMPEG_ERRORS = (
    "no decoder found for",
    "decoder not found",
)


class PermanentExtractionError(RuntimeError):
    """Extraction failed for a reason that retrying cannot fix.

    Retrying costs a full re-read of the source file, so failures that are
    deterministic (a missing file, audio in a codec this ffmpeg build cannot
    decode) should burn one attempt, not the whole retry budget.
    """


_MISSING_DECODER_RE = re.compile(r"no decoder found for:?\s*([A-Za-z0-9_]+)", re.IGNORECASE)


def _missing_decoder(err_text: str) -> str:
    """Pull the codec name out of ffmpeg's 'no decoder found for: ac4' line."""
    match = _MISSING_DECODER_RE.search(err_text)
    return match.group(1) if match else ""


# A second ffmpeg used only when the default build cannot decode a stream.
#
# Broadcasters ship Dolby AC-4 on ATSC 3.0, and stock ffmpeg (including Ubuntu's
# 6.1.1) has no AC-4 decoder, so those recordings could never be transcribed.
# Recent ffmpeg git builds do decode it. This host has one inside the SageTV
# container; scripts/install-ac4-ffmpeg.sh copies it out to bin/.
#
# It is deliberately a *fallback* rather than the default: the primary ffmpeg
# already handles every other recording here, and silently swapping the binary
# for thousands of working files buys nothing and risks regressions.
_AC4_FFMPEG_ENV = "FFMPEG_AC4_BIN"
_fallback_cache: "dict[str, Optional[str]]" = {}


def _decodes(binary: str, codec: str) -> bool:
    """True if ``binary`` advertises a decoder for ``codec``."""
    try:
        out = subprocess.run(
            [binary, "-hide_banner", "-decoders"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    # Lines look like " A....D ac4   Dolby AC-4"; match the name column exactly
    # so a codec whose *description* mentions ac4 cannot produce a false hit.
    return re.search(rf"^\s*\S+\s+{re.escape(codec)}\s", out, re.MULTILINE) is not None


def find_fallback_ffmpeg(codec: str = "ac4") -> Optional[str]:
    """Locate an ffmpeg that can decode ``codec``, or None.

    Cached per codec: each probe spawns ffmpeg, and extraction runs per job.
    Checks an explicit override, then a binary shipped next to the repo, then
    PATH -- and verifies the decoder really exists rather than trusting the
    filename.
    """
    if codec in _fallback_cache:
        return _fallback_cache[codec]

    repo_bin = Path(__file__).resolve().parents[3] / "bin" / "ffmpeg-ac4"
    candidates = [
        os.environ.get(_AC4_FFMPEG_ENV, "").strip(),
        str(repo_bin),
        shutil.which("ffmpeg-ac4") or "",
    ]
    resolved: Optional[str] = None
    for cand in candidates:
        if not cand or not os.path.isfile(cand) or not os.access(cand, os.X_OK):
            continue
        if _decodes(cand, codec):
            logger.info("Using %s as %s-capable ffmpeg fallback", cand, codec)
            resolved = cand
            break
    if resolved is None:
        logger.debug("No fallback ffmpeg with a %s decoder found", codec)
    _fallback_cache[codec] = resolved
    return resolved


# Limit ffmpeg threads to avoid starving SageTV/Channels DVR.
DEFAULT_FFMPEG_THREADS = 4


class AudioExtractor:
    """Extracts audio from video files using ffmpeg."""

    def __init__(self, ssd_temp_dir: str = "/tmp/transcription", ffmpeg_threads: int = DEFAULT_FFMPEG_THREADS):
        self.ssd_temp_dir = ssd_temp_dir
        self.ffmpeg_threads = ffmpeg_threads
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
        # Pre-check: bail early if the source file no longer exists
        if not os.path.exists(video_path):
            raise PermanentExtractionError(f"Source file not found: {video_path}")

        suffix = ""
        if start_seconds is not None and start_seconds > 0:
            suffix = f"_from{int(start_seconds)}"
        output_path = os.path.join(self.ssd_temp_dir, f"{recording_id}{suffix}.wav")

        if os.path.exists(output_path):
            logger.debug("Audio already extracted: %s", output_path)
            return output_path

        logger.info("Extracting audio: %s -> %s (start=%.1fs)",
                     video_path, output_path, start_seconds or 0)
        rc, err_text = await self._run_ffmpeg("ffmpeg", video_path, output_path, start_seconds)

        if rc != 0:
            # A codec the default build cannot decode is not necessarily fatal:
            # AC-4 (ATSC 3.0) needs a newer ffmpeg, which may be installed
            # alongside. Retry once with it before giving up on the recording.
            codec = _missing_decoder(err_text)
            if codec:
                fallback = find_fallback_ffmpeg(codec)
                if fallback:
                    logger.info(
                        "Default ffmpeg cannot decode '%s'; retrying %s with %s",
                        codec, os.path.basename(video_path), os.path.basename(fallback),
                    )
                    rc, err_text = await self._run_ffmpeg(
                        fallback, video_path, output_path, start_seconds
                    )

        if rc != 0:
            err = self._summarise_ffmpeg_error(err_text)
            message = f"ffmpeg extraction failed (rc={rc}): {err}"
            lowered = err_text.lower()
            if any(marker in lowered for marker in _PERMANENT_FFMPEG_ERRORS):
                codec = _missing_decoder(err_text)
                if codec:
                    message = (
                        f"unsupported audio codec '{codec}' — no available ffmpeg "
                        f"build has a decoder for it, so the audio cannot be read: "
                        f"{video_path}"
                    )
                raise PermanentExtractionError(message)
            raise RuntimeError(message)

        logger.info("Audio extracted: %s (%.1f MB)", output_path,
                     os.path.getsize(output_path) / (1024 * 1024))
        return output_path

    async def _run_ffmpeg(
        self,
        binary: str,
        video_path: str,
        output_path: str,
        start_seconds: Optional[float],
    ) -> "tuple[int, str]":
        """Run one extraction attempt. Returns (returncode, stderr text)."""
        # Use nice/ionice to lower extraction priority so DVR playback isn't affected.
        cmd = ["nice", "-n", "19", "ionice", "-c", "3", binary,
               "-threads", str(self.ffmpeg_threads)]
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
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        return proc.returncode, stderr.decode(errors="replace")

    @staticmethod
    def _summarise_ffmpeg_error(err_text: str) -> str:
        """Reduce ffmpeg stderr to the lines that actually say what went wrong."""
        err_lines = [l for l in err_text.splitlines()
                     if l.strip() and not l.startswith("  ")
                     and "ffmpeg version" not in l
                     and "lib" not in l[:6]
                     and "configuration:" not in l
                     and "built with" not in l]
        return "\n".join(err_lines[-5:]) if err_lines else err_text[-300:]

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

    async def extract_region(
        self,
        audio_path: str,
        start: float,
        end: float,
    ) -> Optional[str]:
        """Slice a [start, end] region from an existing WAV into a temp WAV.

        Returns the path to the sliced clip, or None on failure. Used by the
        gap-fill STT path. Caller is responsible for cleanup().
        """
        if end <= start or not os.path.exists(audio_path):
            return None
        base = Path(audio_path).stem
        clip_path = os.path.join(
            self.ssd_temp_dir,
            f"{base}_gap_{int(start * 1000)}_{int(end * 1000)}.wav",
        )
        cmd = [
            "nice", "-n", "19", "ionice", "-c", "3",
            "ffmpeg", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-i", audio_path,
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            clip_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(clip_path):
            logger.warning(
                "Gap slice failed [%.2f-%.2f]: %s",
                start, end, stderr.decode(errors="replace")[-200:],
            )
            return None
        return clip_path
