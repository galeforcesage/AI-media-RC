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
import re
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
            metadata = await self.fetch_metadata(system, recording_id, job.get("file_path"))
            if metadata is None:
                metadata = {"title": recording_id}
                logger.warning("No metadata found for %s, using recording_id as title", recording_id)
            # Ensure title is present (required by sidecar)
            if not metadata.get("title"):
                metadata["title"] = recording_id

            # 1a. Promote channels-dvr `original_air_epoch` to `air_date` if MCP
            # supplied it (true broadcast date, not the recording date).
            if metadata.get("original_air_epoch") and not metadata.get("air_date_epoch"):
                metadata["air_date"] = metadata["original_air_epoch"]

            # 1b. Fallback: extract record_date from filename if metadata didn't provide it.
            # IMPORTANT: filename gives the *recording* timestamp, not the original
            # air date. Only fill record_date here; leave air_date NULL so a future
            # re-enrich can populate it from MCP without false data sticking.
            if not metadata.get("record_date"):
                fname = job.get("file_path") or recording_id
                parsed_epoch = self._extract_date_from_filename(fname)
                if parsed_epoch:
                    metadata["record_date"] = parsed_epoch
                    logger.info("Extracted record_date from filename for %s: %d", recording_id, parsed_epoch)

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

            # 5b. Move sidecar alongside the original recording file.
            #     This keeps sidecars co-located with recordings whether on
            #     HDD (Channels DVR) or inside a Docker volume (SageTV).
            #     The central sidecars/ dir is just a staging area.
            source_file = job.get("file_path")
            if sidecar_path and source_file:
                try:
                    recording_dir = os.path.dirname(source_file)
                    dest = os.path.join(recording_dir, os.path.basename(sidecar_path))
                    shutil.copy2(sidecar_path, dest)
                    os.unlink(sidecar_path)
                    logger.info("Moved sidecar to recording dir: %s", dest)
                except Exception:
                    # Keep the staging copy if move fails
                    logger.warning("Failed to move sidecar to recording dir", exc_info=True)

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
        self, system: str, recording_id: str, file_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch recording metadata from the appropriate MCP server via JSON-RPC."""
        if system == "sagetv":
            host, port = self._parse_addr(self.sagetv_url)
            tool = "sagetv_get_recording"
            # SageTV filenames: {Title}-{MediaFileID}-{Segment}.ext
            # Extract the MediaFileID from the recording_id. NOTE: for
            # Channels-DVR-imported files the embedded number is the Channels
            # recording ID, not the SageTV MediaFileID, so also pass file_path
            # to let the tool fall back to a filename match.
            parts = recording_id.rsplit("-", 2)
            media_file_id = parts[-2] if len(parts) >= 3 else recording_id
            arguments = {"media_file_id": media_file_id, "file_path": file_path or recording_id}
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
                # Channels-DVR (and others) wrap the payload in an envelope:
                # {"success": true, "data": {...real fields...}, "message": "..."}
                if isinstance(raw, dict) and isinstance(raw.get("data"), dict) and (
                    "success" in raw or "ok" in raw
                ):
                    return raw["data"]
                return raw

            return result

        except Exception:
            logger.exception("Failed to fetch metadata from %s for %s", system, recording_id)
            return None

    @staticmethod
    def _normalize_sagetv_metadata(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a raw SageTV MediaFile into the standard metadata dict.

        ``sagetv_get_recording`` returns the raw SageTV MediaFile object
        (from GetMediaFileForID / GetMediaFiles), whose fields are named
        ``ShowTitle``, ``ChannelName``, ``FileStartTime`` (epoch **ms**), etc.
        This mirrors ``mcp-sagetv``'s ``_slim_recording`` so titles, channel,
        and dates actually populate (previously the wrong camelCase keys were
        read, leaving every SageTV transcript with null metadata).
        """
        data = raw.get("data") or raw
        airing = data.get("Airing") or {}
        show = airing.get("Show") or {}
        channel = airing.get("Channel") or {}

        def _ms_to_s(v: Any) -> Optional[int]:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return None
            if iv <= 0:
                return None
            # SageTV epochs are milliseconds (13 digits); convert to seconds.
            return iv // 1000 if iv > 10_000_000_000 else iv

        title = show.get("ShowTitle") or ""
        ep_title = show.get("ShowEpisode") or ""
        # Imported files often carry ShowTitle = server name (e.g. "SageTV9")
        # with the real name embedded in ShowEpisode as a filename. Recover it.
        if re.match(r"^SageTV\d*$", str(title), re.IGNORECASE) and ep_title and "-" in ep_title:
            parts = ep_title.split("-")
            title = parts[0]
            rest = "-".join(parts[1:])
            rest = re.sub(r"^S\d+E\d+-", "", rest)
            rest = re.sub(r"-\d+-\d+$", "", rest)
            if rest:
                ep_title = rest

        people = show.get("PeopleListInShow") or show.get("People") or []
        actors = []
        if isinstance(people, list):
            for i, person in enumerate(people):
                name = person if isinstance(person, str) else str(person)
                if name:
                    actors.append({"name": name, "role": None, "billing_order": i})

        seg = data.get("SegmentFiles") or []
        if isinstance(seg, str):
            seg = [seg]
        file_path = seg[0] if seg else None

        duration_ms = data.get("FileDuration") or 0
        try:
            duration_s = int(duration_ms) / 1000 if duration_ms else None
        except (TypeError, ValueError):
            duration_s = None

        return {
            "title": title,
            "episode_title": ep_title or None,
            "season": show.get("ShowSeasonNumber"),
            "episode": show.get("ShowEpisodeNumber"),
            "genre": show.get("ShowCategory") or "",
            "channel": channel.get("ChannelName") or None,
            "channel_number": channel.get("ChannelNumber") or None,
            "air_date": _ms_to_s(airing.get("AiringStartTime")),
            "record_date": _ms_to_s(data.get("FileStartTime") or airing.get("AiringStartTime")),
            "duration": duration_s,
            "file_path": file_path,
            "file_size": data.get("Size"),
            "description": show.get("ShowDescription") or None,
            "rating": show.get("ShowParentalRating") or None,
            "source_id": show.get("ShowExternalID") or None,
            "actors": actors,
        }

    @staticmethod
    def _extract_date_from_filename(name: str) -> Optional[int]:
        """Extract record_date epoch from Channels DVR filename pattern.

        Channels DVR filenames end with YYYY-MM-DD-HHMM (e.g.,
        'NCIS S23E19 Deal With the Devil 2026-05-05-1900.mpg').
        Returns epoch seconds or None.
        """
        import re
        # Match YYYY-MM-DD-HHMM at end of stem (strip extension first)
        stem = os.path.splitext(os.path.basename(name))[0] if "/" in name or "\\" in name else name
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})$', stem)
        if m:
            from datetime import datetime
            try:
                dt = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5))
                )
                return int(dt.timestamp())
            except (ValueError, OSError):
                return None
        return None

    @staticmethod
    def _parse_addr(addr: str) -> tuple[str, int]:
        if ":" in addr:
            host, port_str = addr.rsplit(":", 1)
            return host, int(port_str)
        return addr, 8766
