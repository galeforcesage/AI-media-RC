"""
enrichment.py
Post-transcription metadata enrichment pipeline.

After a recording is transcribed, this pipeline:
1. Fetches recording metadata from the appropriate MCP server
2. Extracts actors/cast
3. Splits transcript into 30-second chunks
4. Writes JSON sidecar
5. Inserts into the transcript index
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

import aiohttp

from .transcript_index import TranscriptIndex
from .sidecar import TranscriptSidecar
from . import diarization

logger = logging.getLogger(__name__)


class MetadataEnrichmentPipeline:
    """Enriches transcriptions with metadata and indexes them."""

    def __init__(
        self,
        index: TranscriptIndex,
        sidecar: TranscriptSidecar,
        sagetv_url: str = "127.0.0.1:8766",
        channels_url: str = "127.0.0.1:8767",
    ):
        self.index = index
        self.sidecar = sidecar
        self.sagetv_url = sagetv_url
        self.channels_url = channels_url

    async def enrich(self, job: Dict[str, Any]) -> None:
        """
        Main pipeline entry point. Called after transcription completes.

        job must contain:
          - recording_id: str
          - system: 'sagetv' or 'channelsdvr'
          - segments: list of Whisper segments [{start, end, text}, ...]
          - transcript_text: str (full text)
          - word_count: int
          - language: str
          - confidence: float
          - model: str
          - file_path: str (optional)
        """
        recording_id = job["recording_id"]
        system = job["system"]
        logger.info("Enrichment pipeline started for %s (%s)", recording_id, system)

        try:
            # 1. Fetch metadata from MCP server
            metadata = await self.fetch_metadata(system, recording_id)
            if metadata is None:
                metadata = {"title": recording_id}
                logger.warning("No metadata found for %s, using recording_id as title", recording_id)
            # Ensure title is present (required by sidecar)
            if not metadata.get("title"):
                metadata["title"] = recording_id

            # 2. Extract actors
            actors = metadata.get("actors", [])

            # 2.5 Map anonymous speaker labels to character/role names
            segments = job.get("segments", [])
            speaker_map = {}
            has_speakers = any(s.get("speaker") for s in segments)
            if has_speakers and actors:
                speaker_map, segments = diarization.map_speakers_to_characters(
                    segments, actors
                )

            # 3. Split transcript into 30s chunks (with speaker labels propagated)
            chunks = self.chunk_transcript(segments, window=30)

            # 4. Build transcript dict
            transcript_data = {
                "raw_text": job.get("transcript_text", ""),
                "cleaned_text": job.get("cleaned_text"),
                "word_count": job.get("word_count", 0),
                "language": job.get("language", "en"),
                "confidence": job.get("confidence", 0.0),
                "model": job.get("model", "unknown"),
                "transcribed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "speaker_map": speaker_map if speaker_map else None,
            }

            # 5. Write JSON sidecar
            sidecar_path = self.sidecar.write(
                recording_id=recording_id,
                system=system,
                metadata=metadata,
                transcript=transcript_data,
                chunks=chunks,
                summary=None,  # Summary generated asynchronously later
            )

            # 5b. Copy sidecar alongside the original recording file
            source_file = job.get("file_path")
            if sidecar_path and source_file:
                try:
                    recording_dir = os.path.dirname(source_file)
                    dest = os.path.join(recording_dir, os.path.basename(sidecar_path))
                    shutil.copy2(sidecar_path, dest)
                    logger.info("Copied sidecar to recording dir: %s", dest)
                except Exception:
                    logger.warning("Failed to copy sidecar to recording dir", exc_info=True)

            # 6. Insert into transcript index
            genre = metadata.get("genre", [])
            if isinstance(genre, list):
                genre = ", ".join(genre)

            rec = {
                "recording_id": recording_id,
                "system": system,
                "title": metadata.get("title", ""),
                "episode_title": metadata.get("episode_title"),
                "season": metadata.get("season"),
                "episode": metadata.get("episode"),
                "genre": genre,
                "channel": metadata.get("channel"),
                "channel_number": metadata.get("channel_number"),
                "air_date": metadata.get("air_date"),
                "record_date": metadata.get("record_date"),
                "duration": metadata.get("duration"),
                "file_path": job.get("file_path"),
                "file_size": metadata.get("file_size"),
                "description": metadata.get("description"),
                "rating": metadata.get("rating"),
                "source_id": metadata.get("source_id"),
                "sidecar_path": sidecar_path,
                "transcribed_at": int(time.time()),
            }
            self.index.insert_recording(rec)

            if actors:
                self.index.insert_actors(recording_id, actors)

            if chunks:
                self.index.insert_chunks(recording_id, chunks)

            logger.info(
                "Enrichment complete for %s: %d actors, %d chunks",
                recording_id, len(actors), len(chunks),
            )

        except Exception:
            logger.exception("Enrichment pipeline failed for %s", recording_id)

    def chunk_transcript(
        self, segments: List[Dict[str, Any]], window: int = 30
    ) -> List[Dict[str, Any]]:
        """
        Group Whisper segments into windows of `window` seconds.

        Each segment has: {start: float, end: float, text: str, speaker?: str}
        Returns: [{index, start_time, end_time, text, word_count, speaker}, ...]
        """
        if not segments:
            return []

        chunks: List[Dict[str, Any]] = []
        chunk_idx = 0
        chunk_start = 0.0
        chunk_end = float(window)
        current_texts: list[str] = []
        current_speakers: list[str] = []

        def _flush():
            if current_texts:
                text = " ".join(current_texts)
                # Determine dominant speaker in this chunk
                speaker = None
                if current_speakers:
                    from collections import Counter
                    counts = Counter(s for s in current_speakers if s)
                    if counts:
                        speaker = counts.most_common(1)[0][0]
                chunks.append({
                    "index": chunk_idx,
                    "start_time": chunk_start,
                    "end_time": chunk_end,
                    "text": text,
                    "word_count": len(text.split()),
                    "speaker": speaker,
                })

        for seg in segments:
            seg_mid = (seg["start"] + seg["end"]) / 2.0

            # If this segment belongs to the next window
            while seg_mid >= chunk_end:
                _flush()
                chunk_idx += 1
                chunk_start = chunk_end
                chunk_end = chunk_start + window
                current_texts = []
                current_speakers = []

            current_texts.append(seg["text"].strip())
            current_speakers.append(seg.get("speaker"))

        # Flush remaining
        if current_texts:
            text = " ".join(current_texts)
            speaker = None
            if current_speakers:
                from collections import Counter
                counts = Counter(s for s in current_speakers if s)
                if counts:
                    speaker = counts.most_common(1)[0][0]
            chunks.append({
                "index": chunk_idx,
                "start_time": chunk_start,
                "end_time": max(chunk_end, segments[-1]["end"]),
                "text": text,
                "word_count": len(text.split()),
                "speaker": speaker,
            })

        return chunks

    async def fetch_metadata(
        self, system: str, recording_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch recording metadata from the appropriate MCP server via JSON-RPC."""
        if system == "sagetv":
            host, port = self._parse_addr(self.sagetv_url)
            tool = "sagetv_get_recording"
            # SageTV filenames: {Title}-{MediaFileID}-{Segment}.ext
            # Extract the MediaFileID from the recording_id
            parts = recording_id.rsplit("-", 2)
            media_file_id = parts[-2] if len(parts) >= 3 else recording_id
            arguments = {"media_file_id": media_file_id}
        elif system == "channelsdvr":
            host, port = self._parse_addr(self.channels_url)
            tool = "channels_get_recording"
            arguments = {"recording_id": recording_id}
        else:
            logger.error("Unknown system: %s", system)
            return None

        try:
            reader, writer = await asyncio.open_connection(host, port)
            request = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": arguments,
                },
            }) + "\n"
            writer.write(request.encode())
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            writer.close()
            await writer.wait_closed()

            if not line:
                return None

            resp = json.loads(line.decode())
            result = resp.get("result", {})

            # MCP tools return {content: [{type: "text", text: "..."}]}
            content = result.get("content", [])
            if content and content[0].get("type") == "text":
                raw = json.loads(content[0]["text"])
                # Normalize SageTV response structure
                if system == "sagetv":
                    return self._normalize_sagetv_metadata(raw)
                return raw

            return result

        except Exception:
            logger.exception("Failed to fetch metadata from %s for %s", system, recording_id)
            return None

    @staticmethod
    def _normalize_sagetv_metadata(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize SageTV API response into standard metadata dict."""
        data = raw.get("data") or raw
        airing = data.get("Airing") or data.get("airing") or {}
        show = airing.get("Show") or airing.get("show") or {}
        channel = airing.get("Channel") or airing.get("channel") or {}

        # Extract cast/actors — SageTV returns them in the Show object
        people = show.get("People") or show.get("people") or []
        roles = show.get("Roles") or show.get("roles") or []
        actors = []
        for i, person in enumerate(people):
            name = person if isinstance(person, str) else str(person)
            role = roles[i] if i < len(roles) and roles[i] else None
            # Use role as "character name" when it looks like a character (not "Actor"/"Host")
            actors.append({
                "name": name,
                "role": role if role and role not in ("Actor", "Guest", "Host", "Narrator", "Judge") else None,
                "billing_order": i,
            })

        return {
            "title": show.get("Title") or show.get("title") or "",
            "episode_title": show.get("EpisodeTitle") or show.get("episodeTitle"),
            "season": show.get("SeasonNumber") or show.get("seasonNumber"),
            "episode": show.get("EpisodeNumber") or show.get("episodeNumber"),
            "genre": show.get("Category") or show.get("category") or show.get("Genre") or [],
            "channel": channel.get("CallSign") or channel.get("callSign"),
            "channel_number": channel.get("ChannelNumber") or channel.get("channelNumber"),
            "air_date": airing.get("StartTime") or airing.get("startTime"),
            "record_date": data.get("StartTime") or data.get("startTime"),
            "duration": data.get("Duration") or data.get("duration"),
            "file_path": data.get("SegmentFiles") or data.get("segmentFiles"),
            "file_size": data.get("Size") or data.get("size"),
            "description": show.get("Description") or show.get("description"),
            "rating": show.get("Rated") or show.get("ParentalRating") or show.get("rated"),
            "source_id": show.get("ExternalID") or show.get("externalID"),
            "actors": actors,
        }

    @staticmethod
    def _parse_addr(addr: str) -> tuple[str, int]:
        if ":" in addr:
            host, port_str = addr.rsplit(":", 1)
            return host, int(port_str)
        return addr, 8766
