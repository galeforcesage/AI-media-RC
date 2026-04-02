"""
search.py
Unified search service across SageTV + ChannelsDVR.
Supports per-backend queries, fan-out search, and ranked result merging.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict, List

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
