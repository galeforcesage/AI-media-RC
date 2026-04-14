"""
mcp_client.py
Async JSON-RPC client for connecting to downstream MCP servers
(SageTV, Channels DVR, Linux, etc.).

Provides tool invocation with automatic reconnection.
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class MCPClient:
    """
    Connects to an MCP server over TCP and invokes tools via JSON-RPC 2.0.
    Supports lazy connection with auto-reconnect.
    """

    def __init__(self, host: str, port: int, name: str = "mcp") -> None:
        self.host = host
        self.port = port
        self.name = name
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._req_id = 0

    async def _connect(self) -> None:
        """Establish TCP connection to the MCP server."""
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port, limit=1024 * 1024,  # 1 MB readline limit
            )
            logger.info("MCPClient[%s] connected to %s:%d", self.name, self.host, self.port)
        except (OSError, ConnectionRefusedError) as exc:
            logger.warning("MCPClient[%s] connect failed: %s", self.name, exc)
            self._reader = None
            self._writer = None
            raise ConnectionError(f"Cannot reach {self.name} MCP at {self.host}:{self.port}") from exc

    async def _disconnect(self) -> None:
        """Close the TCP connection."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def close(self) -> None:
        """Public close method."""
        async with self._lock:
            await self._disconnect()

    async def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON-RPC request and return the result."""
        async with self._lock:
            # Lazy connect / reconnect
            if self._writer is None:
                await self._connect()

            self._req_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": method,
                "params": params,
            }
            payload = (json.dumps(request) + "\n").encode()

            try:
                self._writer.write(payload)
                await self._writer.drain()
                line = await asyncio.wait_for(self._reader.readline(), timeout=30.0)
                if not line:
                    raise ConnectionError("Empty response from MCP server")
                response = json.loads(line.decode())
            except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
                logger.warning("MCPClient[%s] RPC failed, reconnecting: %s", self.name, exc)
                await self._disconnect()
                raise ConnectionError(f"MCP RPC to {self.name} failed: {exc}") from exc

            if "error" in response:
                err = response["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise RuntimeError(f"MCP error from {self.name}: {msg}")

            return response.get("result", {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """
        Invoke an MCP tool by name.

        Args:
            tool_name: The tool to call (e.g. "sagetv_pause_playback").
            arguments: Tool arguments dict.

        Returns:
            Parsed result from the tool (the content text parsed as JSON).
        """
        result = await self._rpc("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        })
        # Unwrap MCP content envelope
        content = result.get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "{}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
        return result

    async def read_resource(self, uri: str) -> Any:
        """Read an MCP resource by URI."""
        result = await self._rpc("resources/read", {"uri": uri})
        contents = result.get("contents", [])
        if contents:
            text = contents[0].get("text", "{}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return result

    async def list_tools(self) -> list:
        """List available tools from the server."""
        result = await self._rpc("tools/list", {})
        return result.get("tools", [])

    async def execute(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a command in the format expected by the orchestrator stub interface.
        Maps action names to MCP tool names.

        Args:
            action: Short action name (e.g. "play", "pause", "get_recordings").
            payload: Arguments to pass to the tool.

        Returns:
            Tool result dict.
        """
        tool_name = f"{self.name}_{action}"
        return await self.call_tool(tool_name, payload)
