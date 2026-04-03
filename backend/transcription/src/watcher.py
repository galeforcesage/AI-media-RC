"""
watcher.py
File watchers for SageTV and Channels DVR recording directories.

Monitors directories for new completed recordings,
debounces rapid events, and enqueues transcription jobs.
"""

from __future__ import annotations
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, Set

from .models import TranscriptionJob
from .queue import TranscriptionQueue

logger = logging.getLogger(__name__)

# File extensions to watch
MEDIA_EXTENSIONS = {".mpg", ".ts", ".mkv", ".mp4", ".avi"}
# Debounce: wait this long after last modification before processing
DEBOUNCE_SECONDS = 30
# Poll interval
POLL_INTERVAL = 30


class FileWatcher:
    """Polls a directory for new completed recordings."""

    def __init__(self, name: str, watch_dir: str, system: str, queue: TranscriptionQueue):
        self.name = name
        self.watch_dir = watch_dir
        self.system = system
        self.queue = queue
        self._known_files: Dict[str, float] = {}  # path -> mtime at first sight
        self._pending: Dict[str, float] = {}  # path -> last_modified
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("Watcher '%s' started: %s (%s)", self.name, self.watch_dir, self.system)
        while self._running:
            try:
                self._scan()
            except Exception:
                logger.exception("Watcher '%s' scan error", self.name)
            await asyncio.sleep(POLL_INTERVAL)

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
                logger.debug("New file detected: %s", path_str)
            elif path_str in self._pending:
                # File still changing?
                if mtime != self._pending[path_str]:
                    self._pending[path_str] = mtime
                elif (now - mtime) >= DEBOUNCE_SECONDS:
                    # File stable — recording complete
                    self._pending.pop(path_str, None)
                    self._enqueue(path_str)

    def _enqueue(self, file_path: str) -> None:
        recording_id = Path(file_path).stem
        job = TranscriptionJob(
            system=self.system,
            recording_id=recording_id,
            file_path=file_path,
        )
        self.queue.enqueue(job)
        logger.info("Watcher '%s' queued: %s", self.name, recording_id)
