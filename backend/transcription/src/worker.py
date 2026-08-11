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
from .extractor import AudioExtractor, PermanentExtractionError
from .models import TranscriptMetadata, TranscriptionJob
from .queue import TranscriptionQueue
from .store import MetadataStore
from .whisper_engine import WhisperEngine
from . import diarization
from . import cc_extractor

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
        gpu_idle_timeout: float = 600.0,
    ):
        self.queue = queue
        self.store = store
        self.extractor = extractor
        self.engine = engine
        self.enrichment: Optional[MetadataEnrichmentPipeline] = None
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.gpu_idle_timeout = gpu_idle_timeout
        self._running = False
        self._active_jobs = 0
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._last_job_end = time.monotonic()
        self._reaper_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        # Lower our own process priority so DVR playback/recording isn't starved.
        try:
            os.nice(19)
            logger.info("Transcription worker nice level set to %d", os.nice(0))
        except (OSError, AttributeError):
            pass  # nice() not available on all platforms

        self._running = True
        self._semaphore = asyncio.Semaphore(self.concurrency)

        # Jobs left in 'extracting'/'processing' by a previous run are never
        # touched again, and enqueue() won't re-add their recording, so recover
        # them before starting rather than stranding those recordings forever.
        try:
            recovered = self.queue.reset_stale_jobs()
            if recovered:
                logger.warning("Recovered %d job(s) abandoned by a previous run", recovered)
        except Exception:
            logger.exception("Stale job recovery failed")

        logger.info("Transcription worker started (concurrency=%d, gpu_idle_timeout=%ss)",
                    self.concurrency, self.gpu_idle_timeout)

        if self.gpu_idle_timeout > 0:
            self._reaper_task = asyncio.create_task(self._idle_reaper())

        while self._running:
            try:
                job = self.queue.dequeue()
                if job:
                    await self._semaphore.acquire()
                    self._active_jobs += 1
                    asyncio.create_task(self._process_with_semaphore(job))
                else:
                    await asyncio.sleep(self.poll_interval)
            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(self.poll_interval)

    async def _idle_reaper(self) -> None:
        """Release Whisper/diarization models when the queue has been idle.

        Models are reloaded automatically on the next job. This trades a few
        seconds of reload latency for several GB of VRAM that would otherwise
        sit pinned 24/7 between recordings.
        """
        interval = max(15.0, min(self.gpu_idle_timeout / 4, 60.0))
        while self._running:
            await asyncio.sleep(interval)
            if self._active_jobs > 0:
                continue
            if not (self.engine.loaded or diarization.is_loaded()):
                continue
            idle_for = time.monotonic() - self._last_job_end
            if idle_for < self.gpu_idle_timeout:
                continue
            # Only *pending* work should hold the models: the worker loop will
            # pick it up within poll_interval. Deliberately not checking the
            # 'extracting'/'processing' counts here — those are rows some worker
            # claimed, and a job this process is really running is already
            # covered by _active_jobs above. Rows abandoned by an earlier run
            # stay in those states forever, and treating them as live work
            # pinned several GB of VRAM indefinitely.
            try:
                if self.queue.stats().get("pending"):
                    continue
            except Exception:
                logger.debug("Queue stats unavailable during idle check", exc_info=True)
            logger.info("Idle for %.0fs — releasing GPU models", idle_for)
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, self.engine.unload)
                await loop.run_in_executor(None, diarization.unload)
            except Exception:
                logger.exception("Failed to release GPU models")

    def stop(self) -> None:
        self._running = False
        if self._reaper_task:
            self._reaper_task.cancel()
            self._reaper_task = None
        self.engine.unload()
        diarization.unload()

    async def _process_with_semaphore(self, job: TranscriptionJob) -> None:
        try:
            await self._process(job)
        finally:
            self._active_jobs -= 1
            self._last_job_end = time.monotonic()
            self._semaphore.release()

    async def _process(self, job: TranscriptionJob) -> None:
        logger.info("Processing job %s: %s (%s)", job.job_id, job.recording_id, job.file_path)

        # Check if this is an incremental/live transcription job.
        # Detect from recording_id pattern since ad-hoc attrs don't survive queue round-trip.
        is_incremental = getattr(job, "_incremental", False) or "__inc_" in job.recording_id
        is_final = getattr(job, "_final", False) or job.recording_id.endswith("__final")
        if is_incremental and not getattr(job, "_offset", None):
            # Parse offset from recording_id: base__inc_OFFSET or base__inc_OFFSET__final
            parts = job.recording_id.split("__inc_")
            offset = int(parts[1].replace("__final", "")) if len(parts) > 1 and parts[1].replace("__final", "").isdigit() else 0
        else:
            offset = getattr(job, "_offset", 0)
        if is_incremental:
            base_recording_id = job.recording_id.split("__inc_")[0]
        else:
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
        except PermanentExtractionError as e:
            # Retrying re-reads the whole source file for a guaranteed failure,
            # so fail straight to error and leave a readable reason behind.
            logger.error("Extraction permanently failed for %s: %s", job.job_id, e)
            self.queue.update_status(job.job_id, "error", error=f"Extraction: {e}")
            return
        except Exception as e:
            logger.error("Extraction failed for %s: %s", job.job_id, e)
            self.queue.mark_for_retry(job.job_id, f"Extraction: {e}")
            return

        # Step 1.5: Try closed caption extraction (fast probe, free if no CC).
        # Decision tree:
        #   coverage >= 85% && max_gap < 6s        -> cc      (skip whisper)
        #   coverage < 30% || cc_segments < 20     -> stt     (current behavior)
        #   else                                   -> mixed   (cc + gap-fill stt)
        # Skip CC for incremental jobs - they need whisper on the partial audio.
        loop = asyncio.get_running_loop()
        cc_segments: Optional[list] = None
        cc_analysis: Optional[Dict] = None
        transcription_mode = "stt"
        if not is_incremental and cc_extractor.is_available():
            try:
                cc_segments = await loop.run_in_executor(None, cc_extractor.extract, job.file_path)
            except Exception:
                logger.exception("CC extraction failed for %s (non-blocking)", job.job_id)
                cc_segments = None
            if not cc_segments:
                logger.info("No CC found for %s, falling back to STT", job.recording_id)
            if cc_segments:
                try:
                    total_duration = await self.extractor.get_duration(job.file_path)
                except Exception:
                    total_duration = 0.0
                if total_duration > 0:
                    cc_analysis = cc_extractor.analyze(cc_segments, total_duration)
                    logger.info(
                        "CC analysis for %s: %d segments, %.1f%% coverage, max_gap=%.1fs",
                        job.recording_id, cc_analysis["segment_count"],
                        cc_analysis["coverage_pct"], cc_analysis["max_gap"],
                    )
                    if cc_analysis["coverage_pct"] >= 85.0 and cc_analysis["max_gap"] < 6.0:
                        transcription_mode = "cc"
                    elif cc_analysis["coverage_pct"] >= 30.0 and cc_analysis["segment_count"] >= 20:
                        transcription_mode = "mixed"

        # Step 2: Transcribe according to chosen mode
        if transcription_mode == "cc":
            # Use CC segments verbatim, strip any speaker prefixes for clean text.
            segments = []
            for seg in cc_segments:
                clean_text, _hint = cc_extractor.strip_speaker_prefix(seg["text"])
                segments.append({
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": clean_text,
                    "source": "cc",
                })
            full_text = " ".join(s["text"] for s in segments).strip()
            info = {
                "language": "en",
                "language_probability": 1.0,
                "duration": cc_analysis["last_end"] if cc_analysis else 0.0,
                "model": "cc",
                "elapsed_seconds": 0.0,
                "realtime_factor": 0.0,
            }
            logger.info("CC-only path for %s: %d segments, skipped whisper", job.recording_id, len(segments))
        elif transcription_mode == "mixed":
            # Gap-fill: use CC where it covers, run whisper only on gaps >= 6s.
            cc_clean = []
            for seg in cc_segments:
                clean_text, _hint = cc_extractor.strip_speaker_prefix(seg["text"])
                cc_clean.append({
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": clean_text,
                })
            gaps = cc_extractor.find_gaps(
                cc_clean, cc_analysis["last_end"] or total_duration, min_gap=6.0, pad=0.5,
            )
            gap_total = sum(e - s for s, e in gaps)
            logger.info(
                "Mixed path for %s: %d CC segments + %d gap regions (%.1fs to whisper)",
                job.recording_id, len(cc_clean), len(gaps), gap_total,
            )
            stt_segments: list = []
            mix_start = time.time()
            for gap_start, gap_end in gaps:
                clip_path = await self.extractor.extract_region(audio_path, gap_start, gap_end)
                if not clip_path:
                    continue
                try:
                    new_segs = await loop.run_in_executor(
                        None, self.engine.transcribe_clip, clip_path, gap_start
                    )
                    stt_segments.extend(new_segs)
                except Exception:
                    logger.exception("Gap-fill transcribe failed [%.2f-%.2f]", gap_start, gap_end)
                finally:
                    self.extractor.cleanup(clip_path)
            segments = cc_extractor.merge_cc_stt(cc_clean, stt_segments)
            full_text = " ".join(s["text"] for s in segments).strip()
            mix_elapsed = time.time() - mix_start
            info = {
                "language": "en",
                "language_probability": 1.0,
                "duration": cc_analysis["last_end"] if cc_analysis else total_duration,
                "model": f"cc+{self.engine.model_name}",
                "elapsed_seconds": round(mix_elapsed, 1),
                "realtime_factor": round(mix_elapsed / max(gap_total, 1), 2),
            }
            logger.info(
                "Mixed path complete for %s: %d total segments (cc=%d, stt=%d) in %.1fs",
                job.recording_id, len(segments), len(cc_clean), len(stt_segments), mix_elapsed,
            )
        else:
            try:
                full_text, segments, info = await loop.run_in_executor(
                    None, self.engine.transcribe, audio_path
                )
                # Tag STT segments with source for downstream consumers
                for seg in segments:
                    seg.setdefault("source", "stt")
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
            # All Whisper work for this job is done by now, and diarization is
            # the memory-hungry step. Holding both resident peaks around 6.5 GB
            # of VRAM; dropping Whisper first keeps the peak near 4 GB so a live
            # TV transcoder sharing this GPU has headroom. Nothing here is
            # latency-sensitive (every job is queued or batched) and transcribe()
            # reloads on demand, so the reload cost lands on batch throughput
            # rather than on anything a viewer sees.
            await loop.run_in_executor(None, self.engine.unload)
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
            source=transcription_mode,
        )

        # Step 5: Store (append for incremental, overwrite otherwise)
        self.store.save(meta, append=is_incremental)

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
        """Simple keyword extraction (word frequency, lightly TF-weighted).

        Filters stopwords, common contractions, and high-frequency low-info words
        (discourse markers, hedges, vocatives) so the resulting chip list is
        actually informative rather than full of "it's", "okay", "well".
        """
        stop_words = {
            # articles / aux / prepositions
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "shall", "can", "need", "dare", "ought",
            "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "as", "into", "through", "during", "before", "after", "above", "below",
            "between", "under", "again", "further", "then", "once", "here", "there",
            "when", "where", "why", "how", "all", "each", "every", "both", "few",
            "more", "most", "other", "some", "such", "no", "not", "only", "own",
            "same", "so", "than", "too", "very", "just", "because", "but", "and",
            "or", "if", "while", "that", "this", "these", "those", "it", "i",
            "you", "he", "she", "we", "they", "me", "him", "her", "us", "them",
            "my", "your", "his", "its", "our", "their", "what", "which", "who",
            "whom", "whose",
            # contractions (kept whole AND split forms below also covered)
            "i'm", "i've", "i'll", "i'd", "you're", "you've", "you'll", "you'd",
            "he's", "he'll", "he'd", "she's", "she'll", "she'd", "it's", "it'll",
            "we're", "we've", "we'll", "we'd", "they're", "they've", "they'll",
            "that's", "there's", "there're", "here's", "what's", "who's", "let's",
            "don't", "doesn't", "didn't", "won't", "wouldn't", "shouldn't",
            "couldn't", "can't", "cannot", "isn't", "aren't", "wasn't", "weren't",
            "haven't", "hasn't", "hadn't", "ain't", "y'all",
            # discourse markers / hedges / vocatives that swamp TV dialogue
            "okay", "ok", "yeah", "yes", "yep", "nope", "nah", "oh", "um", "uh",
            "er", "ah", "ahh", "hmm", "huh", "mm", "mmm", "hey", "hi", "hello",
            "well", "now", "like", "really", "actually", "basically", "literally",
            "maybe", "probably", "sort", "kind", "thing", "things", "stuff",
            "way", "time", "day", "days", "year", "years", "one", "two", "first",
            "still", "even", "much", "many", "long", "good", "bad", "back",
            "right", "left", "down", "up", "out", "off", "over", "about",
            "around", "away", "got", "get", "gets", "gotten", "getting",
            "go", "goes", "going", "gonna", "wanna", "gotta", "went", "come",
            "comes", "coming", "came", "see", "sees", "seeing", "saw", "seen",
            "look", "looks", "looking", "looked", "say", "says", "said",
            "saying", "tell", "tells", "told", "telling", "think", "thinks",
            "thought", "thinking", "know", "knows", "knew", "known", "knowing",
            "want", "wants", "wanted", "wanting", "make", "makes", "made",
            "making", "take", "takes", "took", "taken", "taking", "give",
            "gives", "gave", "given", "giving", "put", "puts", "putting",
            "let", "lets", "letting", "keep", "keeps", "kept", "keeping",
            "please", "thanks", "thank", "sorry", "sure", "fine", "great",
            "little", "big", "old", "new", "people", "man", "men", "woman",
            "women", "guy", "guys", "thing", "something", "anything",
            "everything", "nothing", "someone", "anyone", "everyone", "nobody",
            "somebody", "anybody", "everybody",
        }
        # Frequency map
        freq: Dict[str, int] = {}
        for raw in text.lower().split():
            w = raw.strip(".,!?;:'\"()-—–")
            if not w or len(w) < 4 or w in stop_words:
                continue
            # also reject the part before an apostrophe if that's a stopword
            # (e.g. "don" from "don't" wouldn't normally show but be safe)
            if "'" in w and w.split("'", 1)[0] in stop_words:
                continue
            freq[w] = freq.get(w, 0) + 1
        # Require minimum support so single mentions don't dominate
        sorted_words = sorted(
            ((w, c) for w, c in freq.items() if c >= 2),
            key=lambda x: -x[1],
        )
        return [w for w, _ in sorted_words[:top_n]]
