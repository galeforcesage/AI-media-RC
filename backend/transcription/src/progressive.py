"""Progressive CC/ASR evidence reconciliation for live transcripts."""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import cc_extractor

logger = logging.getLogger(__name__)


def caption_snapshot_is_usable(segments: List[Dict]) -> bool:
    """Reject decoder garbage while allowing short, newly-started caption feeds."""
    texts = [re.sub(r"\s+", " ", str(s.get("text", "")).strip()) for s in segments]
    texts = [t for t in texts if t]
    if not texts:
        return False
    durations = [
        max(0.0, float(segment.get("end", 0)) - float(segment.get("start", 0)))
        for segment in segments
    ]
    last_end = max(float(segment.get("end", 0)) for segment in segments)
    if last_end > 6 * 3600:
        return False
    if len(durations) == 1 and durations[0] > 60:
        return False
    if durations and sum(duration > 30 for duration in durations) / len(durations) > 0.05:
        return False
    sample = " ".join(texts[:200])
    alpha = sum(ch.isalpha() for ch in sample)
    digits = sum(ch.isdigit() for ch in sample)
    if alpha < max(8, len(sample) * 0.25):
        return False
    if digits > alpha and len(texts) >= 5:
        return False
    if len(texts) >= 10 and len(set(texts)) / len(texts) < 0.15:
        return False
    return True


def resolve_text(cc_text: str, asr_text: str) -> tuple[str, str, float]:
    cc_text = (cc_text or "").strip()
    asr_text = (asr_text or "").strip()
    if cc_text and asr_text:
        ratio = difflib.SequenceMatcher(None, cc_text.lower(), asr_text.lower()).ratio()
        return cc_text, "cc_preferred", round(0.75 + 0.24 * ratio, 3)
    if cc_text:
        return cc_text, "cc", 0.82
    return asr_text, "asr", 0.65


class LiveTranscriptMonitor:
    """Poll growing recordings for CC without blocking the file watchers."""

    def __init__(self, store, index, interval: float = 10.0):
        self.store = store
        self.index = index
        self.interval = interval
        self.watchers = []
        self._seen: Dict[str, tuple] = {}
        self._embedded_last_run: Dict[str, float] = {}
        self._embedded_last_size: Dict[str, int] = {}
        self._running = False

    def register(self, watcher) -> None:
        self.watchers.append(watcher)

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        while self._running:
            active: Dict[str, str] = {}
            for watcher in self.watchers:
                for path in watcher.active_files():
                    active[path] = watcher.system
            for path, system in active.items():
                try:
                    await self._poll_path(loop, path, system)
                except Exception:
                    logger.exception("Live caption poll failed for %s", path)
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self._running = False

    async def _poll_path(self, loop, media_path: str, system: str) -> None:
        path = Path(media_path)
        recording_id = path.stem
        sidecar = path.with_suffix(".srt")
        segments: Optional[List[Dict]] = None
        signature = None

        if sidecar.exists() and sidecar.stat().st_size:
            stat = sidecar.stat()
            signature = ("srt", stat.st_mtime_ns, stat.st_size)
            if self._seen.get(media_path) == signature:
                return
            text = await loop.run_in_executor(
                None, lambda: sidecar.read_text(encoding="utf-8", errors="replace")
            )
            segments = cc_extractor._parse_srt(text)
        else:
            stat = path.stat()
            signature = ("embedded", stat.st_mtime_ns, stat.st_size)
            if self._seen.get(media_path) == signature:
                return
            segments = await self._extract_embedded(loop, path, media_path)

        self._seen[media_path] = signature
        if segments and not caption_snapshot_is_usable(segments):
            logger.warning("Rejected malformed live captions for %s", recording_id)
            segments = None
            # A SageTV-generated sidecar can be malformed even when usable CEA
            # captions remain embedded in the recording. Try the same path used
            # for Channels before leaving the recording to ASR only.
            if sidecar.exists():
                segments = await self._extract_embedded(loop, path, media_path)
        if not segments or not caption_snapshot_is_usable(segments):
            return

        self.store.ingest_evidence(
            recording_id, system, segments, evidence="cc", final=False
        )
        resolved = self.store.get_segments(recording_id)
        self.index.upsert_live_transcript(
            recording_id, system, media_path, resolved,
            transcript_state=self.store.get_transcript_state(recording_id),
        )
        logger.info("Live CC updated %s: %d segments", recording_id, len(segments))

    async def _extract_embedded(self, loop, path: Path, media_path: str):
        # Embedded extraction decodes the growing MPEG. Run less frequently
        # than SRT ingestion to protect DVR disks when several shows record.
        now = time.time()
        stat = path.stat()
        if now - self._embedded_last_run.get(media_path, 0) < 45:
            return None
        previous_size = self._embedded_last_size.get(media_path, 0)
        if previous_size and stat.st_size - previous_size < 64 * 1024 * 1024:
            return None
        self._embedded_last_run[media_path] = now
        self._embedded_last_size[media_path] = stat.st_size
        return await loop.run_in_executor(None, cc_extractor.extract, media_path)
