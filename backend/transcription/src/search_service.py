"""
search_service.py
Cross-metadata transcript search API.

Provides the interface consumed by the orchestrator's LLM service
for transcript-aware reasoning.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional

from .transcript_index import TranscriptIndex

logger = logging.getLogger(__name__)


class TranscriptSearchService:
    """High-level search API over the transcript index."""

    def __init__(self, index: TranscriptIndex):
        self.index = index

    def search(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Full-text search with optional metadata filters."""
        results = self.index.search_transcripts(
            query=query, filters=filters, limit=limit, offset=offset,
        )
        return {
            "results": results,
            "total": len(results),
            "query": query,
            "filters": filters or {},
        }

    def search_actor(self, actor_name: str, limit: int = 20) -> Dict[str, Any]:
        """Find recordings featuring a specific actor."""
        results = self.index.search_by_actor(actor_name, limit=limit)
        return {
            "results": results,
            "total": len(results),
            "actor": actor_name,
        }

    def get_recording_summary(self, recording_id: str) -> Dict[str, Any]:
        """Return full enriched summary for a recording."""
        recording = self.index.get_recording(recording_id)
        if not recording:
            return {"error": "Recording not found", "recording_id": recording_id}

        # Get actors
        actors = self.index._conn.execute(
            "SELECT actor_name, role, billing_order FROM actors WHERE recording_id = ? ORDER BY billing_order",
            (recording_id,),
        ).fetchall()

        # Get summary
        summary_row = self.index._conn.execute(
            "SELECT * FROM transcript_summaries WHERE recording_id = ?",
            (recording_id,),
        ).fetchone()

        result = dict(recording)
        result["actors"] = [dict(a) for a in actors]
        if summary_row:
            result["summary"] = dict(summary_row)
        else:
            result["summary"] = None

        return result

    def suggest_context(self, query: str, max_chunks: int = 5) -> str:
        """
        Search transcripts and format top results as an LLM context string.

        Returns a formatted string suitable for injection into the LLM system prompt.
        """
        results = self.index.search_transcripts(query=query, limit=max_chunks)
        if not results:
            return ""

        lines = []
        for r in results:
            title = r.get("title", "Unknown")
            ep = r.get("episode_title", "")
            start = r.get("start_time", 0)
            snippet = r.get("snippet", "").replace("<b>", "").replace("</b>", "")

            time_str = _format_time(start)
            if ep:
                lines.append(f'From "{title}" - "{ep}" at {time_str}: {snippet}')
            else:
                lines.append(f'From "{title}" at {time_str}: {snippet}')

        return "\n".join(lines)


def _format_time(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
