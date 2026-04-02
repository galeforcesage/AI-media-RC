"""
client.py
MCP client for SageTV integration.

Responsibilities:
- Manage connection lifecycle
- Provide async command execution
- Normalize responses for the Orchestrator
"""

from __future__ import annotations
import asyncio
from typing import Any, Dict, Optional


class SageTVClient:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 9000)
        self.connected = False
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Establish connection to SageTV backend."""
        # Placeholder for real connection logic
        await asyncio.sleep(0.05)
        self.connected = True

    async def disconnect(self) -> None:
        """Close connection to SageTV backend."""
        await asyncio.sleep(0.05)
        self.connected = False

    async def execute(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a SageTV command.

        Args:
            action: Command name (e.g., "play", "pause", "seek")
            payload: Command parameters

        Returns:
            Dict containing result or error.
        """
        if not self.connected:
            return {"error": "SageTV client not connected"}

        async with self._lock:
            try:
                handler = getattr(self, f"_cmd_{action}", None)
                if handler is None:
                    return {"error": f"Unknown SageTV command '{action}'"}

                return await handler(payload)

            except Exception as exc:
                return {"error": str(exc)}

    # -------------------------
    # Command Handlers (stubs)
    # -------------------------

    async def _cmd_play(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "action": "play", "payload": payload}

    async def _cmd_pause(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "action": "pause", "payload": payload}

    async def _cmd_seek(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        position = payload.get("position")
        return {"status": "ok", "action": "seek", "position": position}
