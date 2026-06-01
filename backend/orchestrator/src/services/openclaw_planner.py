"""OpenClaw planner adapter.

This scaffold keeps behavior safe during rollout by delegating to AgentLoop
until OpenClaw-native planning is integrated.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from utils.logger import get_logger
from services.mcp_tool_registry import MCPToolRegistry
from services.planner_base import PlannerBase
from services.openclaw_runtime import OpenClawRuntime

logger = get_logger(__name__)


class OpenClawPlanner(PlannerBase):
    """Initial OpenClaw adapter with safe fallback behavior."""

    def __init__(self, orchestrator: Any, fallback_planner: PlannerBase) -> None:
        self._orch = orchestrator
        self._fallback = fallback_planner
        self._tool_registry = MCPToolRegistry(orchestrator)
        self._runtime = OpenClawRuntime(
            orchestrator.config.get("agent", {}).get("openclaw", {})
        )

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
        cfg = self._orch.config.get("agent", {}).get("openclaw", {})
        enabled = bool(cfg.get("enabled", False))
        strict = bool(cfg.get("strict", False))
        timeout_ms = int(cfg.get("timeout_ms", 30000) or 30000)

        if not enabled:
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

        logger.info("OpenClaw planner selected (native stub mode)")
        if status_callback:
            await status_callback("Planning with OpenClaw")

        tools, _schemas = await self._tool_registry.discover_openai_tools(
            systems=systems,
            domains=domains or [],
            temporal=temporal or "",
        )

        payload = {
            "query": user_query,
            "transcript_context": transcript_context,
            "semantic_context": semantic_context,
            "systems": systems or [],
            "temporal": temporal,
            "domains": domains or [],
            "tools": tools,
            "max_plan_depth": int(cfg.get("max_plan_depth", 5) or 5),
        }

        try:
            runtime_result = await self._runtime.execute(payload, timeout_ms=timeout_ms)
            runtime_result.setdefault("planner", "openclaw-native")
            runtime_result.setdefault("openai_tools_offered", len(tools))
            return runtime_result
        except Exception as exc:
            logger.warning("OpenClaw runtime failed: %s", exc)
            if strict:
                return {
                    "status": "error",
                    "response": f"OpenClaw runtime failed: {exc}",
                    "model": "openclaw",
                    "iterations": 1,
                    "planner": "openclaw-native-error",
                    "openai_tools_offered": len(tools),
                }

            if status_callback:
                await status_callback("OpenClaw runtime unavailable, falling back")

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
                result.setdefault("planner", "openclaw-fallback-runtime")
                result.setdefault("openclaw_error", str(exc))
            return result
