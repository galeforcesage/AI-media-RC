"""
client.py
Client for the orchestrator to interact with the Unified Session Manager.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class SessionManagerClient:
    """Async HTTP client for the Unified Session Manager."""

    def __init__(self, base_url: str = "http://127.0.0.1:8769"):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        session = await self._ensure_session()
        async with session.get(f"{self.base_url}{path}", params=params) as resp:
            return await resp.json()

    async def _post(self, path: str, body: Optional[Dict] = None) -> Dict:
        session = await self._ensure_session()
        async with session.post(f"{self.base_url}{path}", json=body or {}) as resp:
            return await resp.json()

    # ------------------------------------------------------------------
    # Device operations
    # ------------------------------------------------------------------

    async def list_devices(self) -> Dict:
        return await self._get("/devices")

    async def get_device(self, device_id: str) -> Dict:
        return await self._get(f"/devices/{device_id}")

    async def add_device(self, system: str, name: str, ip: str = "", platform: str = "unknown") -> Dict:
        return await self._post("/devices", {
            "system": system,
            "friendly_name": name,
            "ip_address": ip,
            "platform": platform,
        })

    async def delete_device(self, device_id: str) -> Dict:
        session = await self._ensure_session()
        async with session.delete(f"{self.base_url}/devices/{device_id}") as resp:
            return await resp.json()

    async def set_default(self, device_id: str) -> Dict:
        return await self._post(f"/devices/{device_id}/default")

    async def get_default(self) -> Dict:
        return await self._get("/devices/default")

    # ------------------------------------------------------------------
    # Session resolution
    # ------------------------------------------------------------------

    async def resolve_session(self, device_id: str = "") -> Dict:
        if device_id:
            return await self._get(f"/sessions/resolve/{device_id}")
        return await self._get("/sessions/resolve")

    async def list_sessions(self) -> Dict:
        return await self._get("/sessions")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> Dict:
        return await self._get("/health")
