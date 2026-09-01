"""Shared planner interface for query execution backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, Optional


class PlannerBase(ABC):
    """Abstract planner interface used by the orchestrator query flow."""

    @abstractmethod
    async def run(
        self,
        user_query: str,
        transcript_context: str = "",
        semantic_context: str = "",
        systems: list[str] | None = None,
        temporal: str = "",
        domains: list[str] | None = None,
        entity_store: Any | None = None,
        conversation_context: str = "",
        status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        token_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Run a planner-backed query and return normalized agent output."""
