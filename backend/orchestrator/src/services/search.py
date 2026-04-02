"""
search.py
Unified search service across SageTV + ChannelsDVR.
Supports per-backend queries, fan-out search, and ranked result merging.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SearchService:
    """
    Provides local and remote search over indexed metadata.
    Returns ranked results from all configured backends.
    """

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def search_programs(self, target: str, query: str) -> Dict[str, Any]:
        """
        Search programs on a single backend.

        Args:
            target: Backend name ("sagetv" or "channels").
            query: The search string.
        """
        logger.info("Searching programs: target=%s query=%s", target, query)
        try:
            return await self.orchestrator.execute(
                f"{target}.search", {"query": query}
            )
        except Exception as exc:
            logger.exception("search_programs failed")
            return {"error": str(exc)}

    async def search_all(self, query: str) -> Dict[str, Any]:
        """
        Fan-out search across all backends concurrently.
        Returns a dict keyed by backend name.
        """
        logger.info("Fan-out search: query=%s", query)
        targets = ("sagetv", "channels")
        tasks = [self.search_programs(t, query) for t in targets]
        raw = await asyncio.gather(*tasks, return_exceptions=True)

        results: Dict[str, Any] = {}
        for target, res in zip(targets, raw):
            if isinstance(res, Exception):
                logger.error("search_all error for %s: %s", target, res)
                results[target] = {"error": str(res)}
            else:
                results[target] = res

        return results

    async def search_ranked(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Search all backends and return a merged, ranked list of results.

        Results are sorted by relevance score (if available) and capped at *limit*.
        """
        logger.info("Ranked search: query=%s limit=%d", query, limit)
        all_results = await self.search_all(query)

        merged: List[Dict[str, Any]] = []
        for target, result in all_results.items():
            if "error" in result:
                continue
            items = result.get("results", result.get("items", []))
            for item in items:
                item["_source"] = target
                merged.append(item)

        merged.sort(key=lambda r: r.get("score", 0), reverse=True)
        return merged[:limit]

    # ------------------------------------------------------------------
    # Transcript cross-metadata search (PRD Section 13)
    # ------------------------------------------------------------------

    async def transcript_search(
        self, query: str, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Search transcripts via direct JSON-RPC to the transcription MCP server.
        """
        logger.info("Transcript search: query=%s filters=%s", query, filters)
        try:
            import asyncio, json
            args = {"query": query}
            if filters:
                args.update(filters)

            reader, writer = await asyncio.open_connection("127.0.0.1", 8770)
            request = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "transcript_cross_search",
                    "arguments": args,
                },
            }) + "\n"
            writer.write(request.encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            writer.close()
            await writer.wait_closed()

            if not line:
                return {"error": "Empty response from transcription server"}

            resp = json.loads(line.decode())
            result = resp.get("result", {})
            content = result.get("content", [])
            if content and content[0].get("type") == "text":
                return json.loads(content[0]["text"])
            return result
        except Exception as exc:
            logger.exception("transcript_search failed")
            return {"error": str(exc)}

    async def inject_transcript_context(
        self, query: str, max_chunks: int = 5
    ) -> str:
        """
        Search transcripts for relevant content and format as an LLM
        context string. Called by the LLM service before generating a response.
        """
        logger.info("Injecting transcript context for: %s", query)
        try:
            result = await self.transcript_search(query, filters=None)
            data = result.get("data", result)
            results = data.get("results", [])
            if not results:
                return ""

            lines = []
            for r in results[:max_chunks]:
                title = r.get("title", "Unknown")
                ep = r.get("episode_title", "")
                start = r.get("start_time", 0)
                snippet = r.get("snippet", "").replace("<b>", "").replace("</b>", "")
                time_str = self._format_time(start)
                if ep:
                    lines.append(f'From "{title}" - "{ep}" at {time_str}: {snippet}')
                else:
                    lines.append(f'From "{title}" at {time_str}: {snippet}')

            return "\n".join(lines)
        except Exception:
            logger.exception("inject_transcript_context failed")
            return ""

    @staticmethod
    def _format_time(seconds: float) -> str:
        s = int(seconds)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"
