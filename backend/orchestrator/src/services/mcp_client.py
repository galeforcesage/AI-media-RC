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
import subprocess
import time
from typing import Any, Dict, Optional

from utils.tracing import span as trace_span
from utils.metrics import mcp_calls_total, mcp_call_duration, mcp_errors_total

logger = logging.getLogger(__name__)

# Map MCP client names to watchdog service names
_WATCHDOG_SERVICE_MAP = {
    "sagetv": "mcp-sagetv",
    "channels": "mcp-channels",
    "linux": "mcp-linux",
}
_WATCHDOG_SCRIPT = "/home/USER_HOME/AI-media-RC/scripts/watchdog.sh"
# Minimum seconds between auto-restart attempts per service
_RESTART_COOLDOWN = 120


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
        self._last_restart: float = 0.0  # epoch of last auto-restart attempt

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
            # Auto-restart: try to bring the service back via watchdog
            await self._try_auto_restart()
            raise ConnectionError(f"Cannot reach {self.name} MCP at {self.host}:{self.port}") from exc

    async def _try_auto_restart(self) -> None:
        """Attempt to restart the MCP service via watchdog if cooldown allows."""
        svc_name = _WATCHDOG_SERVICE_MAP.get(self.name)
        if not svc_name:
            return
        now = time.monotonic()
        if now - self._last_restart < _RESTART_COOLDOWN:
            logger.info("MCPClient[%s] auto-restart skipped (cooldown %ds remaining)",
                        self.name, int(_RESTART_COOLDOWN - (now - self._last_restart)))
            return
        self._last_restart = now
        logger.warning("MCPClient[%s] auto-restarting %s via watchdog", self.name, svc_name)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", _WATCHDOG_SCRIPT, "restart", svc_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                logger.info("MCPClient[%s] auto-restart succeeded: %s",
                            self.name, stdout.decode().strip())
            else:
                logger.warning("MCPClient[%s] auto-restart failed (code %d): %s",
                               self.name, proc.returncode, stderr.decode().strip())
        except Exception as e:
            logger.warning("MCPClient[%s] auto-restart error: %s", self.name, e)

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
        mcp_calls_total.inc(labels={"server": self.name, "tool": tool_name})
        start = time.time()
        try:
            async with trace_span("mcp.call_tool", {"mcp.server": self.name, "mcp.tool": tool_name}) as s:
                result = await self._rpc("tools/call", {
                    "name": tool_name,
                    "arguments": arguments or {},
                })
                # Unwrap MCP content envelope
                content = result.get("content", [])
                if content and isinstance(content, list):
                    text = content[0].get("text", "{}")
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        parsed = {"raw": text}
                else:
                    parsed = result
                s.set_attribute("result_size", len(str(parsed)))
                mcp_call_duration.observe(time.time() - start)
                return parsed
        except Exception as exc:
            mcp_errors_total.inc(labels={"server": self.name, "tool": tool_name})
            mcp_call_duration.observe(time.time() - start)
            raise

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

    async def ping(self) -> bool:
        """Return True if the MCP server is reachable (TCP connect)."""
        try:
            async with self._lock:
                if self._writer is None:
                    await self._connect()
            return self._writer is not None
        except (ConnectionError, OSError):
            return False

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
