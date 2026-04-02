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
import time
from typing import Any, Dict, List, Optional, Set

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
        # Event subscription state
        self._subscriptions: Dict[asyncio.StreamWriter, Set[str]] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._last_active_ids: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self._poll_task = asyncio.create_task(self._event_poll_loop())
        logger.info("SageTV MCP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
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

                response = await self._dispatch(request, writer)
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            self._subscriptions.pop(writer, None)
            writer.close()
            logger.debug("MCP client disconnected: %s", addr)

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, request: Dict[str, Any], writer: asyncio.StreamWriter = None) -> Dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        # Methods that need writer context
        if method == "tools/call":
            try:
                result = await self._handle_call_tool(params, writer)
                return {"jsonrpc": "2.0", "id": req_id, "result": result}
            except Exception as exc:
                logger.exception("Error in %s", method)
                return self._error_response(req_id, -32000, str(exc))

        handler = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_list_tools,
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
                "resources": {"subscribe": True, "listChanged": False},
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

    async def _handle_call_tool(self, params: Dict, writer: asyncio.StreamWriter = None) -> Dict:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        # ---- Event subscription tools (need writer context) ----
        if tool_name == "sagetv_subscribe_events":
            return self._handle_subscribe(arguments, writer)
        if tool_name == "sagetv_unsubscribe_events":
            return self._handle_unsubscribe(arguments, writer)

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

            # ---- Post-execution event triggers ----
            if result.get("success"):
                await self._fire_mutation_event(tool_name, arguments)

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
            {"uri": "sagetv://favorites", "name": "Favorites (Recording Rules)", "mimeType": "application/json"},
            {"uri": "sagetv://system/status", "name": "System Status", "mimeType": "application/json"},
            {"uri": "sagetv://recordings/{mediaFileId}", "name": "Recording by ID", "mimeType": "application/json",
             "description": "Hydrated MediaFile + Airing + Show"},
            {"uri": "sagetv://airings/{airingId}", "name": "Airing by ID", "mimeType": "application/json",
             "description": "Broadcast instance with Show + Channel"},
            {"uri": "sagetv://shows/{showExternalId}", "name": "Show by External ID", "mimeType": "application/json",
             "description": "Program metadata by external ID"},
            {"uri": "sagetv://channels/{stationId}", "name": "Channel by Station ID", "mimeType": "application/json",
             "description": "Channel/station info by station ID"},
        ]}

    async def _handle_read_resource(self, params: Dict) -> Dict:
        uri = params.get("uri", "")

        # Static collection resources
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
        if handler is not None:
            data = await handler()
            return {"contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps(data)}]}

        # Parameterized resources (entity by ID)
        data = await self._resolve_parameterized_resource(uri)
        if data is not None:
            return {"contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps(data)}]}

        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps({"error": f"Unknown resource: {uri}"})}]}

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
    # Parameterized resource resolution
    # ------------------------------------------------------------------

    async def _resolve_parameterized_resource(self, uri: str) -> Any:
        """Resolve sagetv://recordings/{id}, sagetv://airings/{id}, etc."""
        if uri.startswith("sagetv://recordings/") and uri != "sagetv://recordings/scheduled":
            media_file_id = uri.split("/")[-1]
            return await self.sagex.call("GetMediaFileForID", [media_file_id])
        if uri.startswith("sagetv://airings/"):
            airing_id = uri.split("/")[-1]
            return await self.sagex.call("GetAiringForID", [airing_id])
        if uri.startswith("sagetv://shows/"):
            show_id = uri.split("/")[-1]
            return await self.sagex.call("GetShowForExternalID", [show_id])
        if uri.startswith("sagetv://channels/") and uri != "sagetv://channels":
            station_id = uri.split("/")[-1]
            return await self.sagex.call("GetChannelForStationID", [station_id])
        return None

    # ------------------------------------------------------------------
    # Event subscription handling
    # ------------------------------------------------------------------

    def _handle_subscribe(self, arguments: Dict, writer: asyncio.StreamWriter) -> Dict:
        """Register a client for event notifications."""
        if writer is None:
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": False, "error": "no_connection",
                    "message": "Event subscription requires a persistent connection",
                })}],
                "isError": True,
            }
        events = arguments.get("events", ["*"])
        if writer not in self._subscriptions:
            self._subscriptions[writer] = set()
        self._subscriptions[writer].update(events)
        logger.info("Client subscribed to events: %s", events)
        return {
            "content": [{"type": "text", "text": json.dumps({
                "success": True,
                "message": f"Subscribed to events: {', '.join(events)}",
                "data": {"subscribed": list(self._subscriptions[writer])},
            })}],
            "isError": False,
        }

    def _handle_unsubscribe(self, arguments: Dict, writer: asyncio.StreamWriter) -> Dict:
        """Unregister a client from event notifications."""
        if writer is None or writer not in self._subscriptions:
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "success": True, "message": "No active subscriptions",
                })}],
                "isError": False,
            }
        events = arguments.get("events")
        if events:
            self._subscriptions[writer] -= set(events)
            if not self._subscriptions[writer]:
                del self._subscriptions[writer]
        else:
            del self._subscriptions[writer]
        return {
            "content": [{"type": "text", "text": json.dumps({
                "success": True, "message": "Unsubscribed",
            })}],
            "isError": False,
        }

    async def _fire_mutation_event(self, tool_name: str, arguments: Dict) -> None:
        """Fire events triggered by mutation tools."""
        event_map = {
            "sagetv_set_media_file_property": ("recording.updated", {
                "mediaFileId": arguments.get("media_file_id"),
                "property": arguments.get("key"),
                "value": arguments.get("value"),
            }),
            "sagetv_delete_media_file": ("recording.deleted", {
                "mediaFileId": arguments.get("media_file_id"),
            }),
            "sagetv_set_watched": ("recording.updated", {
                "airingId": arguments.get("airing_id"),
                "watched": arguments.get("watched", True),
            }),
            "sagetv_set_archived": ("recording.updated", {
                "mediaFileId": arguments.get("media_file_id"),
                "archived": arguments.get("archived", True),
            }),
        }
        entry = event_map.get(tool_name)
        if entry and self._subscriptions:
            event_type, event_data = entry
            await self._notify_subscribers(event_type, event_data)

    # ------------------------------------------------------------------
    # Event polling loop
    # ------------------------------------------------------------------

    async def _event_poll_loop(self) -> None:
        """Background task polling SageTV for recording state changes."""
        poll_interval = 10  # seconds
        while True:
            try:
                await asyncio.sleep(poll_interval)
                if not self._subscriptions:
                    continue
                await self._poll_active_recordings()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Event poll error")

    async def _poll_active_recordings(self) -> None:
        """Detect recording.started / recording.completed by tracking active recordings."""
        try:
            active = await self.sagex.call("GetCurrentlyRecordingMediaFiles") or []
        except Exception:
            return

        active_ids: Dict[str, Any] = {}
        for mf in (active if isinstance(active, list) else []):
            mid = str(mf.get("MediaFileID") or mf.get("mediaFileID") or "")
            if mid:
                active_ids[mid] = mf

        if self._last_active_ids is not None:
            prev = set(self._last_active_ids.keys())
            curr = set(active_ids.keys())

            for mid in curr - prev:
                await self._notify_subscribers("recording.started", {
                    "mediaFileId": mid, "mediaFile": active_ids[mid],
                })
            for mid in prev - curr:
                await self._notify_subscribers("recording.completed", {
                    "mediaFileId": mid,
                })

        self._last_active_ids = active_ids

    async def _notify_subscribers(self, event_type: str, data: Dict) -> None:
        """Push a JSON-RPC notification to all subscribed clients."""
        dead_writers: List[asyncio.StreamWriter] = []
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/event",
            "params": {
                "type": event_type,
                "data": data,
                "timestamp": int(time.time() * 1000),
            },
        }
        payload = (json.dumps(notification) + "\n").encode()

        for writer, events in self._subscriptions.items():
            if event_type in events or "*" in events:
                try:
                    writer.write(payload)
                    await writer.drain()
                except (ConnectionResetError, ConnectionError, OSError):
                    dead_writers.append(writer)

        for w in dead_writers:
            self._subscriptions.pop(w, None)
            logger.debug("Removed dead subscriber")

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
