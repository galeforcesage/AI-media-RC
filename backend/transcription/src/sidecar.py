"""
sidecar.py
Read/write transcript JSON sidecar files per Appendix Y schema.

Each transcribed recording produces a .transcript.json sidecar alongside
the .txt and .vtt outputs.
"""

from __future__ import annotations
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
REQUIRED_TOP_KEYS = {"version", "recording_id", "system", "metadata", "transcript", "chunks"}


class TranscriptSidecar:
    """Reads and writes .transcript.json sidecar files."""

    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        recording_id: str,
        system: str,
        metadata: Dict[str, Any],
        transcript: Dict[str, Any],
        chunks: List[Dict[str, Any]],
        summary: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Write a .transcript.json sidecar. Returns the output path."""
        if not recording_id:
            raise ValueError("recording_id is required")
        if system not in ("sagetv", "channelsdvr"):
            raise ValueError(f"Invalid system: {system}")
        if not metadata.get("title"):
            raise ValueError("metadata.title is required")
        if not transcript.get("raw_text") and not transcript.get("word_count"):
            raise ValueError("transcript.raw_text or word_count is required")

        doc = {
            "version": SCHEMA_VERSION,
            "recording_id": recording_id,
            "system": system,
            "metadata": metadata,
            "transcript": transcript,
            "chunks": chunks,
            "summary": summary,
        }

        filename = f"{recording_id}.transcript.json"
        out_path = self.output_dir / filename

        # Atomic write: write to temp, then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.output_dir), suffix=".tmp", prefix=".sidecar_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(out_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.info("Wrote sidecar: %s", out_path)
        return str(out_path)

    def read(self, path: str) -> Dict[str, Any]:
        """Read and validate a sidecar file."""
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)

        missing = REQUIRED_TOP_KEYS - set(doc.keys())
        if missing:
            raise ValueError(f"Sidecar missing required keys: {missing}")

        return doc

    def find_sidecars(self, directory: str) -> List[str]:
        """Recursively find all .transcript.json files in a directory."""
        results = []
        for root, _dirs, files in os.walk(directory):
            for f in files:
                if f.endswith(".transcript.json"):
                    results.append(os.path.join(root, f))
        return sorted(results)

    def reindex_all(self, directory: str, index) -> int:
        """Read all sidecars and insert into the transcript index. Returns count."""
        paths = self.find_sidecars(directory)
        count = 0
        for path in paths:
            try:
                doc = self.read(path)
                rec = self._doc_to_recording(doc, path)
                index.insert_recording(rec)

                actors = doc.get("metadata", {}).get("actors", [])
                if actors:
                    index.insert_actors(doc["recording_id"], actors)

                if doc.get("chunks"):
                    index.insert_chunks(doc["recording_id"], doc["chunks"])

                summary = doc.get("summary")
                if summary:
                    index.insert_summary(doc["recording_id"], summary)

                count += 1
            except Exception:
                logger.exception("Failed to reindex sidecar: %s", path)

        logger.info("Reindexed %d recordings from %s", count, directory)
        return count

    def _doc_to_recording(self, doc: Dict, sidecar_path: str) -> Dict[str, Any]:
        """Convert a sidecar document to a recordings table row dict."""
        meta = doc.get("metadata", {})
        trans = doc.get("transcript", {})
        genre = meta.get("genre", [])
        if isinstance(genre, list):
            genre = ", ".join(genre)

        return {
            "recording_id": doc["recording_id"],
            "system": doc["system"],
            "title": meta.get("title", ""),
            "episode_title": meta.get("episode_title"),
            "season": meta.get("season"),
            "episode": meta.get("episode"),
            "genre": genre,
            "channel": meta.get("channel"),
            "channel_number": meta.get("channel_number"),
            "air_date": self._iso_to_epoch(meta.get("air_date")),
            "record_date": self._iso_to_epoch(meta.get("record_date")),
            "duration": meta.get("duration"),
            "file_path": meta.get("file_path"),
            "file_size": meta.get("file_size"),
            "description": meta.get("description"),
            "rating": meta.get("rating"),
            "source_id": meta.get("source_id"),
            "sidecar_path": sidecar_path,
            "transcribed_at": self._iso_to_epoch(trans.get("transcribed_at")),
        }

    @staticmethod
    def _iso_to_epoch(val) -> Optional[int]:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return int(val)
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, AttributeError):
            return None
