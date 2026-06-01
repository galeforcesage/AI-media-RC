"""OpenClaw planner adapter.

This scaffold keeps behavior safe during rollout by delegating to AgentLoop
until OpenClaw-native planning is integrated.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from utils.logger import get_logger
from services.planner_base import PlannerBase

logger = get_logger(__name__)


class OpenClawPlanner(PlannerBase):
    """Initial OpenClaw adapter with safe fallback behavior."""

    def __init__(self, orchestrator: Any, fallback_planner: PlannerBase) -> None:
        self._orch = orchestrator
        self._fallback = fallback_planner

    async def run(
        self,
        user_query: str,
        transcript_context: str = "",
        semantic_context: str = "",
        systems: list[str] | None = None,
        temporal: str = "",
        domains: list[str] | None = None,
        entity_store: Any | None = None,
        status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        token_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        logger.info("OpenClaw planner selected (fallback mode)")

        # Phase 1 safety: delegate to AgentLoop until OpenClaw runtime
        # integration is ready. This keeps production behavior unchanged.
        result = await self._fallback.run(
            user_query,
            transcript_context=transcript_context,
            semantic_context=semantic_context,
            systems=systems,
            temporal=temporal,
            domains=domains,
            entity_store=entity_store,
            status_callback=status_callback,
            token_callback=token_callback,
        )
        if isinstance(result, dict):
            result.setdefault("planner", "openclaw-fallback")
        return result
