"""
transcription_queue.py
Async transcription queue for batching and sequencing STT jobs.
Provides enqueue, start/stop lifecycle, and result callbacks.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class TranscriptionQueue:
    """
    Async queue that processes audio files sequentially through a worker function.
    Supports start/stop lifecycle and an optional result callback.
    """

    def __init__(
        self,
        worker: Callable[[str], Awaitable[Dict[str, Any]]],
        on_result: Callable[[str, Dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """
        Args:
            worker: Async function that takes an audio_path and returns a result dict.
            on_result: Optional async callback invoked with (audio_path, result) on completion.
        """
        self.worker = worker
        self.on_result = on_result
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._processed: int = 0
        self._errors: int = 0

    @property
    def pending(self) -> int:
        """Number of items waiting in the queue."""
        return self._queue.qsize()

    @property
    def processed(self) -> int:
        """Total items successfully processed."""
        return self._processed

    @property
    def errors(self) -> int:
        """Total items that encountered errors."""
        return self._errors

    async def start(self) -> None:
        """Start the worker loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Transcription queue started")

    async def stop(self) -> None:
        """Signal the worker loop to stop and wait for it to finish."""
        if not self._running:
            return
        self._running = False
        await self._queue.put("__STOP__")
        if self._task:
            await self._task
            self._task = None
        logger.info(
            "Transcription queue stopped (processed=%d, errors=%d)",
            self._processed,
            self._errors,
        )

    async def enqueue(self, audio_path: str) -> None:
        """Add an audio file to the processing queue."""
        await self._queue.put(audio_path)
        logger.debug("Enqueued: %s (pending=%d)", audio_path, self._queue.qsize())

    async def _run(self) -> None:
        """Internal worker loop — processes items one at a time."""
        while self._running:
            audio_path = await self._queue.get()
            if audio_path == "__STOP__":
                self._queue.task_done()
                break
            try:
                logger.info("Processing: %s", audio_path)
                result = await self.worker(audio_path)
                self._processed += 1
                if self.on_result:
                    await self.on_result(audio_path, result)
            except Exception:
                self._errors += 1
                logger.exception("Worker failed for: %s", audio_path)
            finally:
                self._queue.task_done()
