"""
watcher.py
File watchers for SageTV and Channels DVR recording directories.

Monitors directories for new completed recordings,
debounces rapid events, and enqueues transcription jobs.

Supports live/incremental transcription: when exactly one recording
is in progress, it enqueues incremental jobs on the stable portion
of the file so transcription keeps up with the recording.
"""

from __future__ import annotations
import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional, Set

from .models import TranscriptionJob
from .queue import TranscriptionQueue

logger = logging.getLogger(__name__)

# File extensions to watch
MEDIA_EXTENSIONS = {".mpg", ".ts", ".mkv", ".mp4", ".avi"}
# Paths to skip (streaming cache, temp files)
SKIP_PATH_FRAGMENTS = {"/Streaming/", "\\Streaming\\"}
# Filename patterns to skip (HLS chunk files etc)
SKIP_FILENAME_RE = re.compile(r"^stream\d+\.[a-z0-9]+$", re.IGNORECASE)
# Intermediate files produced by other tools mid-job (chunk_00001.mkv etc)
SKIP_CHUNK_FILENAME_RE = re.compile(r"^chunk[_-]?\d+\.[a-z0-9]+$", re.IGNORECASE)
# Directory names holding another tool's in-progress work rather than finished
# recordings. These get created, filled and deleted while we are still polling,
# so anything queued from them is usually gone by the time a worker picks it up.
# Matching is deliberately case-sensitive: tool-generated directories are
# lowercase ("campusquest-chunks", "uwwpsa.work"), while media folders are
# title-cased ("Scratch (2001)"). Wrongly ignoring a recording loses its
# transcript silently, whereas wrongly accepting scratch costs one fast failure,
# so err toward accepting.
SKIP_DIR_NAMES = {"scratch", "tmp", "temp", "incomplete", "partial"}
SKIP_DIR_SUFFIX_RE = re.compile(
    r"[-_.](?:chunks?|scratch|work|working|assemble|tmp|temp|incomplete|partial)$"
)


def is_ignored_media_path(entry: Path, watch_dir: str) -> bool:
    """True if this path is a scratch/intermediate artefact rather than a recording."""
    path_str = str(entry)
    if any(fragment in path_str for fragment in SKIP_PATH_FRAGMENTS):
        return True
    if SKIP_FILENAME_RE.match(entry.name) or SKIP_CHUNK_FILENAME_RE.match(entry.name):
        return True

    try:
        relative = entry.relative_to(watch_dir)
    except ValueError:
        relative = entry
    # Only directories are inspected — a recording's own filename may legitimately
    # contain any of these words.
    for part in relative.parts[:-1]:
        if part.startswith("."):
            return True
        if part in SKIP_DIR_NAMES:
            return True
        if SKIP_DIR_SUFFIX_RE.search(part):
            return True
    return False


# Debounce: wait this long after last modification before processing
DEBOUNCE_SECONDS = 30
# Poll interval
POLL_INTERVAL = 30
# Minimum new bytes before queueing an incremental job (~50 MB)
INCREMENTAL_MIN_BYTES = 50 * 1024 * 1024
# Minimum seconds a file must be growing before starting incremental transcription
INCREMENTAL_MIN_AGE = 120


class FileWatcher:
    """Polls a directory for new completed recordings."""

    def __init__(
        self,
        name: str,
        watch_dir: str,
        system: str,
        queue: TranscriptionQueue,
        enable_live: bool = True,
        on_file_deleted=None,
    ):
        self.name = name
        self.watch_dir = watch_dir
        self.system = system
        self.queue = queue
        self.enable_live = enable_live
        self.on_file_deleted = on_file_deleted  # callback(recording_id: str)
        self._known_files: Dict[str, float] = {}  # path -> mtime at first sight
        self._pending: Dict[str, float] = {}  # path -> last_modified
        self._pending_size: Dict[str, int] = {}  # path -> last known size
        self._pending_first_seen: Dict[str, float] = {}  # path -> time first entered pending
        self._live_offset: Dict[str, int] = {}  # path -> bytes already queued for incremental
        self._live_queued: Dict[str, bool] = {}  # path -> has any incremental job been queued
        self._live_job_id: Dict[str, str] = {}  # path -> job_id of active incremental job
        self._running = False

    async def start(self) -> None:
        self._running = True
        # Seed files older than 2 weeks as "known" so they are skipped.
        # Files within the last 2 weeks will be treated as new and checked
        # for missing transcriptions on the first scan.
        self._seed_existing()
        logger.info("Watcher '%s' started: %s (%s, live=%s, seeded=%d old files)",
                     self.name, self.watch_dir, self.system, self.enable_live,
                     len(self._known_files))
        while self._running:
            try:
                self._scan()
            except Exception:
                logger.exception("Watcher '%s' scan error", self.name)
            await asyncio.sleep(POLL_INTERVAL)

    def _seed_existing(self) -> None:
        """Mark files older than 2 weeks as already known so only recent files get queued."""
        watch_path = Path(self.watch_dir)
        if not watch_path.is_dir():
            return
        cutoff = time.time() - (14 * 86400)  # 2 weeks ago
        for entry in watch_path.rglob("*"):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            if is_ignored_media_path(entry, self.watch_dir):
                continue
            try:
                stat = entry.stat()
                if stat.st_mtime < cutoff:
                    self._known_files[str(entry)] = stat.st_mtime
            except OSError:
                continue

    def stop(self) -> None:
        self._running = False

    def _scan(self) -> None:
        watch_path = Path(self.watch_dir)
        if not watch_path.is_dir():
            return

        now = time.time()
        current_files: Set[str] = set()

        for entry in watch_path.rglob("*"):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in MEDIA_EXTENSIONS:
                continue

            path_str = str(entry)

            # Skip streaming caches, HLS chunks and other tools' scratch dirs
            if is_ignored_media_path(entry, self.watch_dir):
                continue

            current_files.add(path_str)

            try:
                stat = entry.stat()
            except OSError:
                continue

            mtime = stat.st_mtime
            size = stat.st_size

            if size == 0:
                continue  # Ignore empty/partial files

            if path_str not in self._known_files:
                # New file — start debounce
                self._known_files[path_str] = mtime
                self._pending[path_str] = mtime
                self._pending_size[path_str] = size
                self._pending_first_seen[path_str] = now
                logger.debug("New file detected: %s", path_str)
            elif path_str in self._pending:
                # File still changing?
                if mtime != self._pending[path_str]:
                    self._pending[path_str] = mtime
                    self._pending_size[path_str] = size
                elif (now - mtime) >= DEBOUNCE_SECONDS:
                    # File stable — recording complete
                    is_incremental_tail = path_str in self._live_queued
                    self._pending.pop(path_str, None)
                    self._pending_size.pop(path_str, None)
                    self._pending_first_seen.pop(path_str, None)
                    prev_offset = self._live_offset.pop(path_str, 0)
                    self._live_queued.pop(path_str, None)
                    self._live_job_id.pop(path_str, None)
                    if is_incremental_tail:
                        # Final job for the remaining tail of the file
                        self._enqueue(path_str, incremental=True, offset=prev_offset, final=True)
                    else:
                        self._enqueue(path_str)

        # Live transcription: if exactly one file is growing, send incremental jobs
        if self.enable_live:
            self._check_live(now)

        # Detect deleted files and notify callback
        if self.on_file_deleted:
            gone = set(self._known_files.keys()) - current_files
            for path_str in gone:
                recording_id = Path(path_str).stem
                logger.info("Watcher '%s' detected deletion: %s", self.name, recording_id)
                try:
                    self.on_file_deleted(recording_id)
                except Exception:
                    logger.exception("Error handling deletion for %s", recording_id)
                del self._known_files[path_str]
                self._pending.pop(path_str, None)
                self._pending_size.pop(path_str, None)
                self._pending_first_seen.pop(path_str, None)
                self._live_offset.pop(path_str, None)
                self._live_queued.pop(path_str, None)
                self._live_job_id.pop(path_str, None)

    def _check_live(self, now: float) -> None:
        """Queue incremental transcription if exactly one recording is in progress.
        
        Only queues a new incremental job when the previous one for this file
        has completed (status != pending/processing), preventing queue flooding.
        """
        growing = [
            p for p in self._pending
            if (now - self._pending_first_seen.get(p, now)) >= INCREMENTAL_MIN_AGE
        ]

        if len(growing) != 1:
            return  # Only activate for exactly one active recording

        path = growing[0]
        size = self._pending_size.get(path, 0)
        offset = self._live_offset.get(path, 0)
        new_bytes = size - offset

        if new_bytes < INCREMENTAL_MIN_BYTES:
            return

        # Check if a previous incremental job for this file is still pending/processing
        prev_job_id = self._live_job_id.get(path)
        if prev_job_id:
            try:
                prev_job = self.queue.get_job(prev_job_id)
                if prev_job and prev_job.status in ("pending", "extracting", "processing"):
                    logger.debug("Watcher '%s' waiting for previous incremental job %s to finish",
                                 self.name, prev_job_id)
                    return
            except Exception:
                pass  # If we can't check, allow queueing

        self._live_offset[path] = size
        self._live_queued[path] = True
        job = self._enqueue(path, incremental=True, offset=offset, final=False)
        if job:
            self._live_job_id[path] = job.job_id
        logger.info(
            "Watcher '%s' queued incremental job: %s (offset=%d, size=%d)",
            self.name, Path(path).stem, offset, size,
        )

    def _enqueue(
        self,
        file_path: str,
        incremental: bool = False,
        offset: int = 0,
        final: bool = False,
    ) -> Optional[TranscriptionJob]:
        recording_id = Path(file_path).stem
        # Skip if a transcript already exists for this recording (non-incremental only)
        if not incremental:
            try:
                existing = self.queue._conn.execute(
                    "SELECT recording_id FROM transcripts WHERE recording_id = ?",
                    (recording_id,),
                ).fetchone()
                if existing:
                    logger.debug("Transcript already exists for %s, skipping", recording_id)
                    return None
            except Exception:
                pass  # Table may not exist yet; let the queue handle dedup
        job = TranscriptionJob(
            system=self.system,
            recording_id=recording_id,
            file_path=file_path,
        )
        # Attach incremental metadata as extra fields in the job
        if incremental:
            suffix = "__final" if final else ""
            job.recording_id = f"{recording_id}__inc_{offset}{suffix}"
            job._incremental = True
            job._offset = offset
            job._final = final
            job._base_recording_id = recording_id
        self.queue.enqueue(job)
        if incremental:
            logger.info("Watcher '%s' queued incremental: %s (offset=%d, final=%s)",
                        self.name, recording_id, offset, final)
        else:
            logger.info("Watcher '%s' queued: %s", self.name, recording_id)
        return job
