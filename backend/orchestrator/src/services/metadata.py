"""
metadata.py
Unified metadata service for SageTV + ChannelsDVR.
Extracts metadata from media files and queries backend APIs.
Supports duration, codecs, resolution, and audio track discovery.
"""

from __future__ import annotations
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from models.metadata import MediaFileMetadata

logger = logging.getLogger(__name__)


class MetadataService:
    """
    Metadata lookup and extraction service.
    Queries backends via the orchestrator and extracts local file metadata.
    """

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def get_program_info(self, target: str, program_id: str) -> Dict[str, Any]:
        """
        Fetch program metadata from a backend.

        Args:
            target: Backend name ("sagetv" or "channels").
            program_id: The program identifier.
        """
        logger.info("Fetching program info: target=%s id=%s", target, program_id)
        try:
            return await self.orchestrator.execute(
                f"{target}.metadata", {"id": program_id}
            )
        except Exception as exc:
            logger.exception("get_program_info failed")
            return {"error": str(exc)}

    async def search(self, target: str, query: str) -> Dict[str, Any]:
        """Search for programs on a backend."""
        logger.info("Metadata search: target=%s query=%s", target, query)
        try:
            return await self.orchestrator.execute(
                f"{target}.search", {"query": query}
            )
        except Exception as exc:
            logger.exception("metadata search failed")
            return {"error": str(exc)}

    async def extract_file_metadata(self, path: str) -> MediaFileMetadata:
        """
        Extract technical metadata from a local media file.

        Uses ffprobe or mediainfo when available; falls back to basic stat.
        """
        logger.info("Extracting file metadata: %s", path)

        meta = MediaFileMetadata(path=path)

        if not os.path.isfile(path):
            logger.warning("File not found: %s", path)
            return meta

        meta.file_size = os.path.getsize(path)

        ext = os.path.splitext(path)[1].lstrip(".").lower()
        meta.container = ext if ext else None

        # Attempt ffprobe extraction
        try:
            probe_result = await self._run_ffprobe(path)
            if probe_result:
                meta.duration = probe_result.get("duration")
                meta.video_codec = probe_result.get("video_codec")
                meta.audio_codec = probe_result.get("audio_codec")
                meta.resolution = probe_result.get("resolution")
                meta.audio_tracks = probe_result.get("audio_tracks", 1)
        except Exception:
            logger.debug("ffprobe extraction failed for %s", path, exc_info=True)

        return meta

    async def _run_ffprobe(self, path: str) -> Dict[str, Any] | None:
        """
        Run ffprobe to extract media stream information.

        Returns a dict with duration, codecs, resolution, audio_tracks,
        or None if ffprobe is not available.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode != 0:
                return None

            import json
            data = json.loads(stdout.decode())
            fmt = data.get("format", {})
            streams = data.get("streams", [])

            video_codec = None
            audio_codec = None
            resolution = None
            audio_tracks = 0

            for s in streams:
                codec_type = s.get("codec_type")
                if codec_type == "video" and video_codec is None:
                    video_codec = s.get("codec_name")
                    w = s.get("width")
                    h = s.get("height")
                    if w and h:
                        resolution = f"{w}x{h}"
                elif codec_type == "audio":
                    audio_tracks += 1
                    if audio_codec is None:
                        audio_codec = s.get("codec_name")

            duration_str = fmt.get("duration")
            duration = float(duration_str) if duration_str else None

            return {
                "duration": duration,
                "video_codec": video_codec,
                "audio_codec": audio_codec,
                "resolution": resolution,
                "audio_tracks": max(audio_tracks, 1),
            }
        except FileNotFoundError:
            logger.debug("ffprobe not found on PATH")
            return None
