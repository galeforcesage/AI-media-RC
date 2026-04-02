"""
SageTV MCP Server
=================
Full MCP server exposing SageTV capabilities via JSON-RPC 2.0.

Connects to SageTV through the sagex-api REST interface (Jetty + sagex-api-services).
Implements all tools from the SageTV Capability Dictionary (Appendix A / G).

Transport: TCP socket with JSON-RPC 2.0
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from .sagex_client import SageXClient
from .tools import TOOL_REGISTRY, Safety

logger = logging.getLogger(__name__)


class SageTVMCPServer:
    """MCP server for SageTV — exposes tools, resources, and prompts."""

    def __init__(self, config: Dict[str, Any]):
        self.host = config.get("mcp_host", "127.0.0.1")
        self.port = config.get("mcp_port", 8766)
        self.sagex = SageXClient(
            base_url=config.get("sagex_url", "http://localhost:8080"),
            username=config.get("sagex_user", "sage"),
            password=config.get("sagex_pass", "frey"),
        )
        self._server: Optional[asyncio.AbstractServer] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("SageTV MCP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("SageTV MCP server stopped")
        await self.sagex.close()

    # ------------------------------------------------------------------
    # Client handling
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = writer.get_extra_info("peername")
        logger.debug("MCP client connected: %s", addr)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line.decode())
                except json.JSONDecodeError:
                    resp = self._error_response(None, -32700, "Parse error")
                    writer.write((json.dumps(resp) + "\n").encode())
                    await writer.drain()
                    continue

                response = await self._dispatch(request)
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            logger.debug("MCP client disconnected: %s", addr)

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        handler = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_list_tools,
            "tools/call": self._handle_call_tool,
            "resources/list": self._handle_list_resources,
            "resources/read": self._handle_read_resource,
            "ping": self._handle_ping,
        }.get(method)

        if handler is None:
            return self._error_response(req_id, -32601, f"Unknown method: {method}")

        try:
            result = await handler(params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            logger.exception("Error in %s", method)
            return self._error_response(req_id, -32000, str(exc))

    # ------------------------------------------------------------------
    # MCP handlers
    # ------------------------------------------------------------------

    async def _handle_initialize(self, params: Dict) -> Dict:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "sagetv-mcp", "version": "1.0.0"},
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
        }

    async def _handle_ping(self, params: Dict) -> Dict:
        return {}

    async def _handle_list_tools(self, params: Dict) -> Dict:
        tools = []
        for name, entry in TOOL_REGISTRY.items():
            tools.append({
                "name": name,
                "description": entry["description"],
                "inputSchema": entry["input_schema"],
            })
        return {"tools": tools}

    async def _handle_call_tool(self, params: Dict) -> Dict:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        entry = TOOL_REGISTRY.get(tool_name)
        if entry is None:
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": False,
                    "error": "unknown_tool",
                    "message": f"Tool '{tool_name}' not found",
                    "suggestions": ["Call tools/list to see available tools"],
                })}],
                "isError": True,
            }

        try:
            result = await entry["handler"](self.sagex, arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": not result.get("success", False),
            }
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": False,
                    "error": "tool_execution_error",
                    "message": str(exc),
                })}],
                "isError": True,
            }

    async def _handle_list_resources(self, params: Dict) -> Dict:
        return {"resources": [
            {"uri": "sagetv://media/recordings", "name": "Recordings", "mimeType": "application/json"},
            {"uri": "sagetv://media/videos", "name": "Imported Videos", "mimeType": "application/json"},
            {"uri": "sagetv://media/now-playing", "name": "Now Playing", "mimeType": "application/json"},
            {"uri": "sagetv://channels", "name": "Channels", "mimeType": "application/json"},
            {"uri": "sagetv://recordings/scheduled", "name": "Scheduled Recordings", "mimeType": "application/json"},
            {"uri": "sagetv://favorites", "name": "Favorites", "mimeType": "application/json"},
            {"uri": "sagetv://system/status", "name": "System Status", "mimeType": "application/json"},
        ]}

    async def _handle_read_resource(self, params: Dict) -> Dict:
        uri = params.get("uri", "")
        handler_map = {
            "sagetv://media/recordings": self._res_recordings,
            "sagetv://media/videos": self._res_videos,
            "sagetv://media/now-playing": self._res_now_playing,
            "sagetv://channels": self._res_channels,
            "sagetv://recordings/scheduled": self._res_scheduled,
            "sagetv://favorites": self._res_favorites,
            "sagetv://system/status": self._res_system_status,
        }
        handler = handler_map.get(uri)
        if handler is None:
            return {"contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps({"error": f"Unknown resource: {uri}"})}]}
        data = await handler()
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(data)}]}

    # ------------------------------------------------------------------
    # Resource handlers
    # ------------------------------------------------------------------

    async def _res_recordings(self) -> Any:
        return await self.sagex.call("GetMediaFiles", ["T"])

    async def _res_videos(self) -> Any:
        return await self.sagex.call("GetMediaFiles", ["V"])

    async def _res_now_playing(self) -> Any:
        return await self.sagex.call("GetCurrentMediaFile")

    async def _res_channels(self) -> Any:
        return await self.sagex.call("GetAllChannels")

    async def _res_scheduled(self) -> Any:
        return await self.sagex.call("GetScheduledRecordings")

    async def _res_favorites(self) -> Any:
        return await self.sagex.call("GetFavorites")

    async def _res_system_status(self) -> Any:
        results = {}
        results["disk"] = await self.sagex.call("GetTotalDiskspaceAvailable")
        results["tuners"] = await self.sagex.call("GetCaptureDevices")
        results["clients"] = await self.sagex.call("GetConnectedClients")
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _error_response(req_id: Any, code: int, message: str) -> Dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }
