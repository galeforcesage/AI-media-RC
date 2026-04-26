"""
Channels DVR HTTP Client
=========================
Async HTTP client for the Channels DVR REST API.

Channels DVR exposes a JSON-based HTTP API on port 8089 (default).
No authentication is required.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class ChannelsDVRClient:
    """Async HTTP wrapper for the Channels DVR REST API."""

    def __init__(self, base_url: str = "http://localhost:8089"):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Generic request helpers
    # ------------------------------------------------------------------

    async def get(self, path: str, params: Optional[Dict] = None) -> Any:
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        logger.debug("GET %s params=%s", url, params)
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            ct = resp.content_type or ""
            if "json" in ct:
                return await resp.json()
            return await resp.text()

    async def post(self, path: str, json_body: Optional[Dict] = None) -> Any:
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        logger.debug("POST %s body=%s", url, json_body)
        async with session.post(url, json=json_body) as resp:
            resp.raise_for_status()
            ct = resp.content_type or ""
            if "json" in ct:
                return await resp.json()
            return await resp.text()

    async def put(self, path: str, json_body: Optional[Dict] = None) -> Any:
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        logger.debug("PUT %s body=%s", url, json_body)
        async with session.put(url, json=json_body) as resp:
            resp.raise_for_status()
            ct = resp.content_type or ""
            if "json" in ct:
                return await resp.json()
            return await resp.text()

    async def delete(self, path: str, params: Optional[Dict] = None) -> Any:
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        logger.debug("DELETE %s params=%s", url, params)
        async with session.delete(url, params=params) as resp:
            resp.raise_for_status()
            ct = resp.content_type or ""
            if "json" in ct:
                return await resp.json()
            return await resp.text()

    # ------------------------------------------------------------------
    # High-level convenience methods
    # ------------------------------------------------------------------

    async def status(self) -> Dict:
        return await self.get("/status")

    async def dvr_info(self) -> Dict:
        return await self.get("/dvr")

    async def get_recordings(self, include_deleted: bool = False) -> List[Dict]:
        if include_deleted:
            return await self.get("/dvr/files", params={"all": "true"})
        return await self.get("/dvr/files")

    async def get_recording(self, file_id: str) -> Dict:
        return await self.get(f"/dvr/files/{file_id}")

    async def get_rules(self) -> List[Dict]:
        return await self.get("/dvr/rules")

    async def get_jobs(self) -> List[Dict]:
        return await self.get("/dvr/jobs")

    async def get_devices(self) -> List[Dict]:
        return await self.get("/devices")

    async def get_channels(self) -> List[Dict]:
        """Aggregate channels from all devices."""
        devices = await self.get_devices()
        channels = []
        for d in devices:
            for ch in d.get("Channels", []):
                ch["DeviceID"] = d.get("DeviceID")
                ch["DeviceName"] = d.get("FriendlyName")
                channels.append(ch)
        return channels

    async def search_epg(self, query: str) -> List[Dict]:
        return await self.get("/dvr/guide/search", params={"q": query})

    async def get_sessions(self) -> List[Dict]:
        return await self.get("/dvr/sessions")

    async def get_clients(self) -> List[Dict]:
        return await self.get("/dvr/clients")
