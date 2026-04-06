"""
worker.py
Transcription processing worker.

Pulls jobs from the queue, extracts audio, runs Whisper,
generates metadata, and stores results.
"""

from __future__ import annotations
import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from .enrichment import MetadataEnrichmentPipeline
from .extractor import AudioExtractor
from .models import TranscriptMetadata, TranscriptionJob
from .queue import TranscriptionQueue
from .store import MetadataStore
from .whisper_engine import WhisperEngine
from . import diarization

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
        self.enrichment: Optional[MetadataEnrichmentPipeline] = None
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self._running = False
        self._active_jobs = 0
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def start(self) -> None:
        # Lower our own process priority so DVR playback/recording isn't starved.
        try:
            os.nice(10)
            logger.info("Transcription worker nice level set to %d", os.nice(0))
        except (OSError, AttributeError):
            pass  # nice() not available on all platforms

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

        # Check if this is an incremental/live transcription job
        is_incremental = getattr(job, "_incremental", False)
        offset = getattr(job, "_offset", 0)
        is_final = getattr(job, "_final", False)
        base_recording_id = getattr(job, "_base_recording_id", job.recording_id)

        # For incremental jobs, compute a time offset from byte offset
        start_seconds = None
        if is_incremental and offset > 0:
            try:
                total_duration = await self.extractor.get_duration(job.file_path)
                file_size = os.path.getsize(job.file_path)
                if file_size > 0 and total_duration > 0:
                    start_seconds = (offset / file_size) * total_duration
                    logger.info("Incremental job: byte offset %d -> %.1fs of %.1fs",
                                offset, start_seconds, total_duration)
            except Exception:
                logger.warning("Could not compute time offset for incremental job, extracting full")

        # Step 1: Extract audio
        try:
            self.queue.update_status(job.job_id, "extracting")
            audio_path = await self.extractor.extract(
                job.file_path, job.recording_id, start_seconds=start_seconds,
            )
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

        # For incremental jobs, shift segment timestamps to absolute time
        if is_incremental and start_seconds and start_seconds > 0:
            for seg in segments:
                seg["start"] = round(seg["start"] + start_seconds, 2)
                seg["end"] = round(seg["end"] + start_seconds, 2)

        # Step 2.5: Speaker diarization (optional — runs if pyannote is available)
        # Skip diarization for non-final incremental jobs (will run on complete file)
        diarization_turns = []
        if diarization.is_available() and not (is_incremental and not is_final):
            try:
                diarization_turns = await loop.run_in_executor(
                    None, diarization.diarize, audio_path
                )
                if diarization_turns:
                    # For final incremental, shift diarization timestamps too
                    if is_incremental and is_final and start_seconds and start_seconds > 0:
                        for turn in diarization_turns:
                            turn["start"] = round(turn["start"] + start_seconds, 2)
                            turn["end"] = round(turn["end"] + start_seconds, 2)
                    diarization.align_speakers_to_segments(segments, diarization_turns)
                    logger.info("Speaker labels assigned to %d segments", len(segments))
            except Exception:
                logger.exception("Diarization failed for %s (non-blocking, continuing without speakers)", job.job_id)

        # Step 3: Generate VTT
        vtt = self.engine.segments_to_vtt(segments)

        # Step 4: Build metadata
        store_id = base_recording_id if is_incremental else job.recording_id
        meta = TranscriptMetadata(
            recording_id=store_id,
            system=job.system,
            title=store_id,  # Will be enriched later via MCP
            duration=info.get("duration", 0),
            word_count=len(full_text.split()),
            transcript=full_text,
            keywords=self._extract_keywords(full_text),
            vtt=vtt,
        )

        # Step 5: Store (append for incremental, overwrite otherwise)
        self.store.save(meta)

        # Step 6: Enrich (only for complete or final incremental jobs)
        if self.enrichment and not (is_incremental and not is_final):
            try:
                await self.enrichment.enrich({
                    "recording_id": store_id,
                    "system": job.system,
                    "segments": [{"start": s.get("start", 0), "end": s.get("end", 0), "text": s.get("text", ""), "speaker": s.get("speaker")} for s in segments],
                    "diarization_turns": diarization_turns,
                    "transcript_text": full_text,
                    "word_count": len(full_text.split()),
                    "language": info.get("language", "en"),
                    "confidence": info.get("language_probability", 0.0),
                    "model": self.engine.model_name,
                    "file_path": job.file_path,
                })
            except Exception:
                logger.exception("Enrichment failed for %s (non-blocking)", job.job_id)

        self.queue.update_status(job.job_id, "done", duration=info.get("duration", 0))

        # Step 7: Cleanup
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
