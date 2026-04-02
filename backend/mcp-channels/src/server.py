"""
Channels DVR MCP Server
========================
Full MCP server exposing Channels DVR capabilities via JSON-RPC 2.0.

Connects to Channels DVR through its native HTTP REST API (port 8089, no auth).
Implements all tools from the Channels DVR Capability Dictionary (Appendix B / G).

Transport: TCP socket with JSON-RPC 2.0
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from .channels_client import ChannelsDVRClient
from .tools import TOOL_REGISTRY, Safety

logger = logging.getLogger(__name__)


class ChannelsDVRMCPServer:
    """MCP server for Channels DVR — exposes tools, resources, and prompts."""

    def __init__(self, config: Dict[str, Any]):
        self.host = config.get("mcp_host", "127.0.0.1")
        self.port = config.get("mcp_port", 8767)
        self.client = ChannelsDVRClient(
            base_url=config.get("channels_url", "http://localhost:8089"),
        )
        self._server: Optional[asyncio.AbstractServer] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("Channels DVR MCP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        await self.client.close()
        logger.info("Channels DVR MCP server stopped")

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("Client connected: %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line.decode())
                except json.JSONDecodeError:
                    writer.write(
                        (json.dumps({
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        }) + "\n").encode()
                    )
                    await writer.drain()
                    continue

                response = await self._dispatch(request)
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error handling client %s", peer)
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info("Client disconnected: %s", peer)

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, request: Dict) -> Dict:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        handler_map = {
            "initialize": self._handle_initialize,
            "ping": self._handle_ping,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "resources/list": self._handle_resources_list,
            "resources/read": self._handle_resources_read,
        }

        handler = handler_map.get(method)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        try:
            result = await handler(params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            logger.exception("Error in method %s", method)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    async def _handle_initialize(self, params: Dict) -> Dict:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "channels-dvr-mcp", "version": "1.0.0"},
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
        }

    async def _handle_ping(self, params: Dict) -> Dict:
        return {}

    async def _handle_tools_list(self, params: Dict) -> Dict:
        tools = []
        for name, spec in TOOL_REGISTRY.items():
            tools.append({
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            })
        return {"tools": tools}

    async def _handle_tools_call(self, params: Dict) -> Dict:
        name = params.get("name", "")
        arguments = params.get("arguments", {})

        if name not in TOOL_REGISTRY:
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": False,
                    "error": "unknown_tool",
                    "message": f"Tool '{name}' not found",
                })}],
                "isError": True,
            }

        spec = TOOL_REGISTRY[name]

        # Safety gate
        if spec["safety"] in (Safety.CONFIRM, Safety.DANGEROUS, Safety.OWNER):
            confirmed = arguments.pop("_confirmed", False)
            if not confirmed:
                return {
                    "content": [{"type": "text", "text": json.dumps({
                        "success": False,
                        "error": "confirmation_required",
                        "message": f"Tool '{name}' requires confirmation (safety={spec['safety'].value}). Re-send with _confirmed=true.",
                        "safety": spec["safety"].value,
                    })}],
                    "isError": False,
                }

        try:
            result = await spec["handler"](self.client, arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False,
            }
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": False,
                    "error": "tool_error",
                    "message": str(exc),
                })}],
                "isError": True,
            }

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    RESOURCES = [
        {"uri": "channels://recordings", "name": "Recordings", "mimeType": "application/json"},
        {"uri": "channels://scheduled", "name": "Scheduled Recordings", "mimeType": "application/json"},
        {"uri": "channels://channels", "name": "Channels", "mimeType": "application/json"},
        {"uri": "channels://now-playing", "name": "Now Playing", "mimeType": "application/json"},
        {"uri": "channels://clients", "name": "Clients", "mimeType": "application/json"},
        {"uri": "channels://jobs", "name": "DVR Jobs", "mimeType": "application/json"},
        {"uri": "channels://storage", "name": "Storage", "mimeType": "application/json"},
        {"uri": "channels://system/status", "name": "System Status", "mimeType": "application/json"},
    ]

    async def _handle_resources_list(self, params: Dict) -> Dict:
        return {"resources": self.RESOURCES}

    async def _handle_resources_read(self, params: Dict) -> Dict:
        uri = params.get("uri", "")
        resource_handlers = {
            "channels://recordings": self._res_recordings,
            "channels://scheduled": self._res_scheduled,
            "channels://channels": self._res_channels,
            "channels://now-playing": self._res_now_playing,
            "channels://clients": self._res_clients,
            "channels://jobs": self._res_jobs,
            "channels://storage": self._res_storage,
            "channels://system/status": self._res_status,
        }

        handler = resource_handlers.get(uri)
        if not handler:
            # Check for parameterized URIs
            if uri.startswith("channels://epg/search/"):
                query = uri.split("channels://epg/search/", 1)[1]
                data = await self.client.search_epg(query)
            elif uri.startswith("channels://recordings/"):
                file_id = uri.split("channels://recordings/", 1)[1]
                data = await self.client.get_recording(file_id)
            else:
                return {
                    "contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps({"error": f"Unknown resource: {uri}"})}]
                }
            return {
                "contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(data)}]
            }

        data = await handler()
        return {
            "contents": [{"uri": uri, "mimeType": "application/json",
                          "text": json.dumps(data)}]
        }

    async def _res_recordings(self):
        return await self.client.get_recordings()

    async def _res_scheduled(self):
        return await self.client.get_rules()

    async def _res_channels(self):
        return await self.client.get_channels()

    async def _res_now_playing(self):
        return await self.client.get_sessions()

    async def _res_clients(self):
        return await self.client.get_clients()

    async def _res_jobs(self):
        return await self.client.get_jobs()

    async def _res_storage(self):
        dvr = await self.client.dvr_info()
        return dvr.get("disk", {})

    async def _res_status(self):
        return await self.client.status()
