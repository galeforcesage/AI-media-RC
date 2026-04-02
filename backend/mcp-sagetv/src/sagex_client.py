"""
sagex_client.py
Async HTTP client for the sagex-api REST interface.

Endpoint format: /sagex/api?c=COMMAND&1=arg1&2=arg2&encoder=json
Uses Basic auth.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)


class SageXClient:
    """Thin async wrapper around the sagex-api REST endpoint."""

    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url.rstrip("/")
        self._auth = aiohttp.BasicAuth(username, password)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(
        self,
        command: str,
        args: Optional[List[str]] = None,
        *,
        context: Optional[str] = None,
        start: Optional[int] = None,
        size: Optional[int] = None,
    ) -> Any:
        """
        Call a SageTV API via sagex-api REST.

        Args:
            command: SageTV API command (e.g. 'GetMediaFiles', 'MediaPlayerAPI.Pause')
            args: Positional arguments (mapped to &1=, &2=, etc.)
            context: UI context for client-specific commands
            start: Paging start index
            size: Paging page size

        Returns:
            Parsed JSON result (the value of the 'Result' key).
        """
        params: Dict[str, str] = {"c": command, "encoder": "json"}

        if args:
            for i, arg in enumerate(args, start=1):
                params[str(i)] = str(arg)

        if context:
            params["context"] = context
        if start is not None:
            params["start"] = str(start)
        if size is not None:
            params["size"] = str(size)

        url = f"{self._base_url}/sagex/api?{urlencode(params)}"
        logger.debug("sagex request: %s", url)

        session = await self._ensure_session()
        try:
            async with session.get(url) as resp:
                if resp.status == 401:
                    raise PermissionError("SageTV API returned 401 Unauthorized")
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return data.get("Result", data)
        except aiohttp.ClientError as exc:
            logger.error("sagex request failed: %s", exc)
            raise ConnectionError(f"SageTV API error: {exc}") from exc
