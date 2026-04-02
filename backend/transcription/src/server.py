"""
server.py
HTTP + MCP resource server for the transcription subsystem.

Exposes:
- REST API for job management and search
- MCP resources: transcript://{system}/{recording_id}, transcript://search/{query}
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, Optional

from .models import TranscriptMetadata
from .queue import TranscriptionQueue
from .store import MetadataStore

logger = logging.getLogger(__name__)


class TranscriptionServer:
    """TCP JSON-RPC server exposing transcription resources and tools."""

    def __init__(self, config: Dict[str, Any], queue: TranscriptionQueue, store: MetadataStore):
        self.host = config.get("mcp_host", "127.0.0.1")
        self.port = config.get("mcp_port", 8770)
        self.queue = queue
        self.store = store
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("Transcription MCP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.debug("Client connected: %s", peer)
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
                            "jsonrpc": "2.0", "id": None,
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
            logger.exception("Client error %s", peer)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, request: Dict) -> Dict:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        handlers = {
            "initialize": self._init,
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "resources/read": self._resources_read,
        }

        handler = handlers.get(method)
        if not handler:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}}
        try:
            result = await handler(params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            logger.exception("Error in %s", method)
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32000, "message": str(exc)}}

    # ------------------------------------------------------------------
    # MCP protocol handlers
    # ------------------------------------------------------------------

    async def _init(self, p):
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "transcription-mcp", "version": "1.0.0"},
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
        }

    async def _ping(self, p):
        return {}

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    TOOLS = [
        {
            "name": "transcript_search",
            "description": "Full-text search across all transcripts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "transcript_get",
            "description": "Get transcript and metadata for a specific recording.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recording_id": {"type": "string"},
                },
                "required": ["recording_id"],
            },
        },
        {
            "name": "transcript_jobs",
            "description": "List transcription job queue status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status (pending/processing/done/error)"},
                },
            },
        },
        {
            "name": "transcript_stats",
            "description": "Get transcription subsystem statistics.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    async def _tools_list(self, p):
        return {"tools": self.TOOLS}

    async def _tools_call(self, params):
        name = params.get("name", "")
        args = params.get("arguments", {})

        if name == "transcript_search":
            results = self.store.search(args.get("query", ""), args.get("limit", 20))
            data = [{"recording_id": r.recording_id, "title": r.title,
                      "episode": r.episode, "word_count": r.word_count,
                      "snippet": r.transcript[:200]} for r in results]
            return self._tool_ok({"results": data, "count": len(data)})

        elif name == "transcript_get":
            meta = self.store.get(args.get("recording_id", ""))
            if not meta:
                return self._tool_err("Transcript not found")
            return self._tool_ok(meta.to_dict())

        elif name == "transcript_jobs":
            status = args.get("status")
            jobs = self.queue.list_jobs(status=status)
            return self._tool_ok({
                "jobs": [j.to_dict() for j in jobs],
                "count": len(jobs),
            })

        elif name == "transcript_stats":
            store_stats = self.store.stats()
            queue_stats = self.queue.stats()
            return self._tool_ok({**store_stats, "queue": queue_stats})

        return self._tool_err(f"Unknown tool: {name}")

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    RESOURCES = [
        {"uri": "transcript://recent", "name": "Recent Transcripts", "mimeType": "application/json"},
        {"uri": "transcript://stats", "name": "Transcription Stats", "mimeType": "application/json"},
        {"uri": "transcript://jobs", "name": "Job Queue", "mimeType": "application/json"},
    ]

    async def _resources_list(self, p):
        return {"resources": self.RESOURCES}

    async def _resources_read(self, params):
        uri = params.get("uri", "")

        if uri == "transcript://recent":
            metas = self.store.list_recent(20)
            data = [{"recording_id": m.recording_id, "title": m.title,
                      "word_count": m.word_count, "duration": m.duration} for m in metas]
            return self._res(uri, data)

        elif uri == "transcript://stats":
            return self._res(uri, {**self.store.stats(), "queue": self.queue.stats()})

        elif uri == "transcript://jobs":
            jobs = self.queue.list_jobs()
            return self._res(uri, [j.to_dict() for j in jobs])

        elif uri.startswith("transcript://search/"):
            query = uri.split("transcript://search/", 1)[1]
            results = self.store.search(query)
            data = [{"recording_id": r.recording_id, "title": r.title,
                      "snippet": r.transcript[:200]} for r in results]
            return self._res(uri, data)

        elif uri.startswith("transcript://") and "/" in uri.replace("transcript://", "", 1):
            # transcript://{system}/{recording_id}
            parts = uri.replace("transcript://", "").split("/", 1)
            if len(parts) == 2:
                meta = self.store.get(parts[1])
                if meta:
                    return self._res(uri, meta.to_dict())
            return self._res(uri, {"error": "Not found"})

        return self._res(uri, {"error": f"Unknown resource: {uri}"})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _tool_ok(self, data):
        return {"content": [{"type": "text", "text": json.dumps({"success": True, "data": data})}], "isError": False}

    def _tool_err(self, msg):
        return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": msg})}], "isError": True}

    def _res(self, uri, data):
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data)}]}
