"""
client.py
MCP client adapter for the orchestrator to connect to the SageTV MCP server.

The orchestrator uses this to send JSON-RPC requests to the SageTV MCP server,
which in turn calls the sagex-api REST endpoint.
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SageTVClient:
    """Async JSON-RPC client that connects to the SageTV MCP server."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.host = config.get("host", "127.0.0.1")
        self.port = config.get("port", 8766)
        self.connected = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._req_id = 0

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port
        )
        self.connected = True
        # Send initialize
        await self._rpc("initialize", {})
        logger.info("Connected to SageTV MCP server at %s:%d", self.host, self.port)

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        self.connected = False

    async def list_tools(self) -> list:
        result = await self._rpc("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            return json.loads(content[0]["text"])
        return result

    async def list_resources(self) -> list:
        result = await self._rpc("resources/list", {})
        return result.get("resources", [])

    async def read_resource(self, uri: str) -> Any:
        result = await self._rpc("resources/read", {"uri": uri})
        contents = result.get("contents", [])
        if contents and "text" in contents[0]:
            return json.loads(contents[0]["text"])
        return result

    async def execute(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Legacy execute interface — maps to call_tool with sagetv_ prefix."""
        tool_name = f"sagetv_{action}" if not action.startswith("sagetv_") else action
        return await self.call_tool(tool_name, payload)

    async def _rpc(self, method: str, params: Dict) -> Dict:
        async with self._lock:
            self._req_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": method,
                "params": params,
            }
            self._writer.write((json.dumps(request) + "\n").encode())
            await self._writer.drain()
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("MCP server closed connection")
            resp = json.loads(line.decode())
            if "error" in resp:
                raise RuntimeError(resp["error"].get("message", str(resp["error"])))
            return resp.get("result", {})
