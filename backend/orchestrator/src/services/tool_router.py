"""
tool_router.py
Routes incoming LLM tool calls to the correct orchestrator service.
Normalizes inputs and outputs across namespaces:
sagetv, channels, system, search, playback.
"""

from __future__ import annotations
import logging
from typing import Any, Dict

from services.playback_controller import PlaybackController
from services.search import SearchService

logger = logging.getLogger(__name__)


class ToolRouter:
    """
    Routes LLM-generated tool calls to the appropriate orchestrator
    command or high-level service method.
    """

    def __init__(
        self,
        orchestrator: Any,
        playback: PlaybackController,
        search: SearchService,
    ) -> None:
        self.orchestrator = orchestrator
        self.playback = playback
        self.search = search

    async def route_tool_call(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Route a tool call from the LLM to the correct handler.

        Args:
            tool_name: Namespaced tool identifier ("namespace.action").
            args: Tool call arguments.

        Returns:
            Normalized result dict.
        """
        if "." not in tool_name:
            logger.warning("Invalid tool name: %s", tool_name)
            return {"error": f"Invalid tool name '{tool_name}': expected 'namespace.action'"}

        namespace, action = tool_name.split(".", 1)
        logger.info("Routing tool call: %s.%s", namespace, action)

        handler = self._namespace_handlers.get(namespace)
        if handler is None:
            logger.warning("Unknown tool namespace: %s", namespace)
            return {"error": f"Unknown namespace '{namespace}'"}

        try:
            result = await handler(self, action, args)
            return self._normalize_output(result)
        except Exception as exc:
            logger.exception("Tool call failed: %s", tool_name)
            return {"error": str(exc)}

    @staticmethod
    def _normalize_output(result: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure the result dict always has a 'status' key."""
        if "status" not in result and "error" not in result:
            result["status"] = "ok"
        return result

    # ------------------------------------------------------------------
    # Namespace handlers
    # ------------------------------------------------------------------

    async def _handle_sagetv(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Forward to SageTV backend via the orchestrator."""
        return await self.orchestrator.execute(f"sagetv.{action}", args)

    async def _handle_channels(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Forward to ChannelsDVR backend via the orchestrator."""
        return await self.orchestrator.execute(f"channels.{action}", args)

    async def _handle_system(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Forward to the system service via the orchestrator."""
        return await self.orchestrator.execute(f"system.{action}", args)

    async def _handle_search(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Route search requests."""
        query = args.get("query", "")
        if action == "all":
            return await self.search.search_all(query)
        target = args.get("target", "sagetv")
        return await self.search.search_programs(target, query)

    async def _handle_playback(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Route playback control requests."""
        target = args.get("target")
        if action == "play":
            return await self.playback.play(target, args)
        if action == "pause":
            return await self.playback.pause(target)
        if action == "stop":
            return await self.playback.stop(target)
        if action == "seek":
            position = args.get("position", 0)
            return await self.playback.seek(position, target)
        if action == "now_playing":
            states = await self.playback.now_playing(target)
            return {k: v.to_dict() for k, v in states.items()}
        return {"error": f"Unknown playback action '{action}'"}

    _namespace_handlers: Dict[str, Any] = {
        "sagetv": _handle_sagetv,
        "channels": _handle_channels,
        "system": _handle_system,
        "search": _handle_search,
        "playback": _handle_playback,
    }
