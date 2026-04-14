"""
resolver.py
Session resolution — maps device_id → client_id → session_id.

Queries the SageTV and Channels DVR MCP servers to find active playback sessions,
then matches them against the device registry.
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from .models import Device, PlaybackContext, PlaybackSession
from .registry import DeviceRegistry

logger = logging.getLogger(__name__)


class SessionResolver:
    """Resolves the active playback session for a given device."""

    def __init__(self, registry: DeviceRegistry, mcp_config: Dict[str, Any]):
        self.registry = registry
        self._sagetv_host = mcp_config.get("sagetv_host", "127.0.0.1")
        self._sagetv_port = mcp_config.get("sagetv_port", 8766)
        self._channels_host = mcp_config.get("channels_host", "127.0.0.1")
        self._channels_port = mcp_config.get("channels_port", 8767)
        # Cache sessions briefly (max 5s)
        self._cache: Dict[str, PlaybackSession] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 5.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(self, device_id: str) -> PlaybackContext:
        """Resolve the full playback context for a device."""
        device = self.registry.get_device(device_id)
        if not device:
            return PlaybackContext(
                device_id=device_id,
                device_name="unknown",
                system="unknown",
                error=f"Device '{device_id}' not found in registry",
            )

        self.registry.touch_device(device_id)

        session = await self._find_session(device)
        return PlaybackContext(
            device_id=device.device_id,
            device_name=device.friendly_name,
            system=device.system,
            session=session,
        )

    async def resolve_default(self) -> PlaybackContext:
        """Resolve context for the default device."""
        device = self.registry.get_default()
        if not device:
            return PlaybackContext(
                device_id="",
                device_name="",
                system="",
                error="No default device configured",
            )
        return await self.resolve(device.device_id)

    async def list_active_sessions(self) -> List[Dict]:
        """Get all active sessions across both systems."""
        sagetv_sessions = await self._query_sagetv_sessions()
        channels_sessions = await self._query_channels_sessions()
        return sagetv_sessions + channels_sessions

    # ------------------------------------------------------------------
    # Session matching
    # ------------------------------------------------------------------

    async def _find_session(self, device: Device) -> Optional[PlaybackSession]:
        # SageTV context devices: query the context directly via MCP
        sagetv_ctx = (device.capabilities or {}).get("sagetv_context")
        if sagetv_ctx and device.system == "sagetv":
            return await self._query_sagetv_context(device, sagetv_ctx)

        if device.system == "sagetv":
            sessions = await self._query_sagetv_sessions()
        elif device.system == "channelsdvr":
            sessions = await self._query_channels_sessions()
        else:
            return None

        # Match by IP or name
        for s in sessions:
            if self._matches_device(device, s):
                return PlaybackSession(
                    device_id=device.device_id,
                    session_id=s.get("session_id", ""),
                    system=device.system,
                    client_id=s.get("client_id", ""),
                    media_id=s.get("media_id", ""),
                    title=s.get("title", ""),
                    episode=s.get("episode", ""),
                    position=s.get("position", 0.0),
                    duration=s.get("duration", 0.0),
                    state=s.get("state", "unknown"),
                    commercial_markers=s.get("commercial_markers", []),
                    extra=s.get("extra", {}),
                )
        return None

    def _matches_device(self, device: Device, session_info: Dict) -> bool:
        """Match a device to a session by IP, name, or client_id."""
        client_ip = session_info.get("ip", "")
        client_name = session_info.get("name", "").lower()
        client_id = session_info.get("client_id", "")

        if device.ip_address and device.ip_address == client_ip:
            return True
        if device.friendly_name and device.friendly_name.lower() in client_name:
            return True
        if client_id and client_id in device.device_id:
            return True
        return False

    # ------------------------------------------------------------------
    # Direct context query (for auto-discovered SageTV devices)
    # ------------------------------------------------------------------

    async def _query_sagetv_context(self, device: Device, context_id: str) -> Optional[PlaybackSession]:
        """Query playback state for a specific SageTV UI context directly."""
        result = await self._mcp_rpc(
            self._sagetv_host, self._sagetv_port,
            "tools/call",
            {"name": "sagetv_get_context_info", "arguments": {"session_id": context_id}},
        )
        content = result.get("content", [])
        if not content:
            return None
        try:
            data = json.loads(content[0].get("text", "{}"))
        except (json.JSONDecodeError, IndexError):
            return None
        if not data.get("success"):
            return None

        info = data.get("data", {})
        state = info.get("state", "idle")
        if state == "idle":
            return None

        return PlaybackSession(
            device_id=device.device_id,
            session_id=context_id,
            system="sagetv",
            client_id=context_id,
            title=info.get("title", ""),
            position=info.get("position", 0),
            duration=info.get("duration", 0),
            state=state,
        )

    # ------------------------------------------------------------------
    # MCP queries
    # ------------------------------------------------------------------

    async def _mcp_rpc(self, host: str, port: int, method: str, params: Dict) -> Dict:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            writer.close()
            await writer.wait_closed()
            if not line:
                return {}
            resp = json.loads(line.decode())
            return resp.get("result", {})
        except Exception as exc:
            logger.warning("MCP RPC to %s:%d failed: %s", host, port, exc)
            return {}

    async def _query_sagetv_sessions(self) -> List[Dict]:
        """Query SageTV MCP for active playback sessions."""
        result = await self._mcp_rpc(
            self._sagetv_host, self._sagetv_port,
            "tools/call",
            {"name": "sagetv_get_now_playing", "arguments": {}},
        )
        content = result.get("content", [])
        if not content:
            return []
        try:
            data = json.loads(content[0].get("text", "{}"))
        except (json.JSONDecodeError, IndexError):
            return []
        if not data.get("success"):
            return []

        raw = data.get("data", [])
        sessions = []
        for item in (raw if isinstance(raw, list) else [raw]):
            sessions.append({
                "session_id": str(item.get("MediaFileID", item.get("AiringID", ""))),
                "client_id": str(item.get("UIContextName", "")),
                "ip": item.get("ClientIP", ""),
                "name": item.get("UIContextName", ""),
                "media_id": str(item.get("MediaFileID", "")),
                "title": item.get("Title", item.get("ShowTitle", "")),
                "episode": item.get("ShowEpisode", ""),
                "position": item.get("MediaTime", 0) / 1000 if item.get("MediaTime") else 0,
                "duration": item.get("Duration", 0) / 1000 if item.get("Duration") else 0,
                "state": "playing" if item.get("IsPlaying") else "paused",
                "system": "sagetv",
            })
        return sessions

    async def _query_channels_sessions(self) -> List[Dict]:
        """Query Channels DVR MCP for active playback sessions."""
        result = await self._mcp_rpc(
            self._channels_host, self._channels_port,
            "tools/call",
            {"name": "channels_get_now_playing", "arguments": {}},
        )
        content = result.get("content", [])
        if not content:
            return []
        try:
            data = json.loads(content[0].get("text", "{}"))
        except (json.JSONDecodeError, IndexError):
            return []
        if not data.get("success"):
            return []

        raw = data.get("data", [])
        sessions = []
        for item in (raw if isinstance(raw, list) else [raw]):
            sessions.append({
                "session_id": str(item.get("ID", item.get("id", ""))),
                "client_id": str(item.get("ClientID", item.get("client_id", ""))),
                "ip": item.get("IP", item.get("ip", "")),
                "name": item.get("Name", item.get("name", "")),
                "media_id": str(item.get("MediaID", item.get("media_id", ""))),
                "title": item.get("Title", ""),
                "episode": item.get("Episode", ""),
                "position": float(item.get("Position", 0)),
                "duration": float(item.get("Duration", 0)),
                "state": item.get("State", "unknown").lower(),
                "commercial_markers": item.get("Commercials", []),
                "system": "channelsdvr",
            })
        return sessions
