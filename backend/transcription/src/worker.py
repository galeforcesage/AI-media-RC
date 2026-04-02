"""
worker.py
Transcription processing worker.

Pulls jobs from the queue, extracts audio, runs Whisper,
generates metadata, and stores results.
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Any, Dict, Optional

from .extractor import AudioExtractor
from .models import TranscriptMetadata, TranscriptionJob
from .queue import TranscriptionQueue
from .store import MetadataStore
from .whisper_engine import WhisperEngine

logger = logging.getLogger(__name__)


class TranscriptionWorker:
    """Async worker that processes transcription jobs."""

    def __init__(
        self,
        queue: TranscriptionQueue,
        store: MetadataStore,
        extractor: AudioExtractor,
        engine: WhisperEngine,
        concurrency: int = 1,
        poll_interval: float = 10.0,
    ):
        self.queue = queue
        self.store = store
        self.extractor = extractor
        self.engine = engine
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self._running = False
        self._active_jobs = 0
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def start(self) -> None:
        self._running = True
        self._semaphore = asyncio.Semaphore(self.concurrency)
        logger.info("Transcription worker started (concurrency=%d)", self.concurrency)

        while self._running:
            try:
                job = self.queue.dequeue()
                if job:
                    await self._semaphore.acquire()
                    asyncio.create_task(self._process_with_semaphore(job))
                else:
                    await asyncio.sleep(self.poll_interval)
            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _process_with_semaphore(self, job: TranscriptionJob) -> None:
        try:
            await self._process(job)
        finally:
            self._semaphore.release()

    async def _process(self, job: TranscriptionJob) -> None:
        logger.info("Processing job %s: %s (%s)", job.job_id, job.recording_id, job.file_path)

        # Step 1: Extract audio
        try:
            self.queue.update_status(job.job_id, "extracting")
            audio_path = await self.extractor.extract(job.file_path, job.recording_id)
            self.queue.update_status(job.job_id, "processing", temp_audio_path=audio_path)
        except Exception as e:
            logger.error("Extraction failed for %s: %s", job.job_id, e)
            self.queue.mark_for_retry(job.job_id, f"Extraction: {e}")
            return

        # Step 2: Transcribe
        try:
            # Run whisper in thread pool to avoid blocking event loop
            loop = asyncio.get_running_loop()
            full_text, segments, info = await loop.run_in_executor(
                None, self.engine.transcribe, audio_path
            )
        except Exception as e:
            logger.error("Transcription failed for %s: %s", job.job_id, e)
            self.queue.mark_for_retry(job.job_id, f"Transcription: {e}")
            self.extractor.cleanup(audio_path)
            return

        # Step 3: Generate VTT
        vtt = self.engine.segments_to_vtt(segments)

        # Step 4: Build metadata
        meta = TranscriptMetadata(
            recording_id=job.recording_id,
            system=job.system,
            title=job.recording_id,  # Will be enriched later via MCP
            duration=info.get("duration", 0),
            word_count=len(full_text.split()),
            transcript=full_text,
            keywords=self._extract_keywords(full_text),
            vtt=vtt,
        )

        # Step 5: Store
        self.store.save(meta)
        self.queue.update_status(job.job_id, "done", duration=info.get("duration", 0))

        # Step 6: Cleanup
        self.extractor.cleanup(audio_path)

        logger.info("Job %s complete: %d words, %.0fs audio",
                     job.job_id, meta.word_count, meta.duration)

    @staticmethod
    def _extract_keywords(text: str, top_n: int = 10) -> list:
        """Simple keyword extraction (word frequency)."""
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "shall", "can", "need", "dare", "ought",
            "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "as", "into", "through", "during", "before", "after", "above", "below",
            "between", "under", "again", "further", "then", "once", "here", "there",
            "when", "where", "why", "how", "all", "each", "every", "both", "few",
            "more", "most", "other", "some", "such", "no", "not", "only", "own",
            "same", "so", "than", "too", "very", "just", "because", "but", "and",
            "or", "if", "while", "that", "this", "it", "i", "you", "he", "she",
            "we", "they", "me", "him", "her", "us", "them", "my", "your", "his",
            "its", "our", "their", "what", "which", "who", "whom",
        }
        words = text.lower().split()
        freq: Dict[str, int] = {}
        for w in words:
            w = w.strip(".,!?;:'\"()-")
            if len(w) < 3 or w in stop_words:
                continue
            freq[w] = freq.get(w, 0) + 1
        sorted_words = sorted(freq.items(), key=lambda x: -x[1])
        return [w for w, _ in sorted_words[:top_n]]
