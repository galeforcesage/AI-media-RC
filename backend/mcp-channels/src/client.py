"""
client.py
MCP client for ChannelsDVR integration.

Responsibilities:
- Manage connection lifecycle
- Provide async command execution
- Normalize responses for the Orchestrator
"""

from __future__ import annotations
import asyncio
from typing import Any, Dict


class ChannelsClient:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 8089)
        self.connected = False
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Establish connection to ChannelsDVR backend."""
        await asyncio.sleep(0.05)
        self.connected = True

    async def disconnect(self) -> None:
        """Close connection to ChannelsDVR backend."""
        await asyncio.sleep(0.05)
        self.connected = False

    async def execute(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a ChannelsDVR command.

        Args:
            action: Command name (e.g., "play", "pause", "seek", "record")
            payload: Command parameters

        Returns:
            Dict containing result or error.
        """
        if not self.connected:
            return {"error": "Channels client not connected"}

        async with self._lock:
            try:
                handler = getattr(self, f"_cmd_{action}", None)
                if handler is None:
                    return {"error": f"Unknown Channels command '{action}'"}

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

    async def _cmd_record(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        channel = payload.get("channel")
        duration = payload.get("duration")
        return {
            "status": "ok",
            "action": "record",
            "channel": channel,
            "duration": duration,
        }
