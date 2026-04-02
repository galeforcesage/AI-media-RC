"""
Linux MCP Server
=================
Privileged MCP server exposing allowlisted Linux system operations via JSON-RPC 2.0.

Capabilities:
- Service status / restart (allowlisted)
- Disk usage, network info, uptime, memory
- Log viewing (allowlisted paths)
- Docker container management (allowlisted)

Transport: TCP socket with JSON-RPC 2.0
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, Optional

from .tools import TOOL_REGISTRY, Safety

logger = logging.getLogger(__name__)


class LinuxMCPServer:
    """MCP server for privileged Linux system operations."""

    def __init__(self, config: Dict[str, Any]):
        self.host = config.get("mcp_host", "127.0.0.1")
        self.port = config.get("mcp_port", 8768)
        self._server: Optional[asyncio.AbstractServer] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("Linux MCP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Linux MCP server stopped")

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
            "serverInfo": {"name": "linux-mcp", "version": "1.0.0"},
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
            result = await spec["handler"](arguments)
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
        {"uri": "linux://system/disk", "name": "Disk Usage", "mimeType": "application/json"},
        {"uri": "linux://system/network", "name": "Network Info", "mimeType": "application/json"},
        {"uri": "linux://system/uptime", "name": "Uptime", "mimeType": "application/json"},
        {"uri": "linux://system/memory", "name": "Memory", "mimeType": "application/json"},
        {"uri": "linux://docker/containers", "name": "Docker Containers", "mimeType": "application/json"},
    ]

    async def _handle_resources_list(self, params: Dict) -> Dict:
        return {"resources": self.RESOURCES}

    async def _handle_resources_read(self, params: Dict) -> Dict:
        uri = params.get("uri", "")
        from . import system

        resource_map = {
            "linux://system/disk": system.disk_usage,
            "linux://system/network": system.network_info,
            "linux://system/uptime": system.uptime,
            "linux://system/memory": system.memory_info,
            "linux://docker/containers": system.docker_ps,
        }

        handler = resource_map.get(uri)
        if not handler:
            return {
                "contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps({"error": f"Unknown resource: {uri}"})}]
            }

        data = await handler()
        return {
            "contents": [{"uri": uri, "mimeType": "application/json",
                          "text": json.dumps(data)}]
        }
