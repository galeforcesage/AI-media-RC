"""
mcp.py
MCP (Model Context Protocol) server transport for the orchestrator.
Defines tools for: query, playback, metadata, search, system.
Supports streaming responses where applicable.
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Tool definition helpers
# ------------------------------------------------------------------

def _tool_def(name: str, description: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Build a JSON-Schema–style MCP tool definition."""
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": parameters,
        },
    }


MCP_TOOLS: List[Dict[str, Any]] = [
    _tool_def(
        "query",
        "Run an LLM text query and optionally synthesize speech",
        {
            "prompt": {"type": "string", "description": "The user prompt"},
            "synthesize": {"type": "boolean", "description": "Generate TTS audio", "default": True},
        },
    ),
    _tool_def(
        "playback",
        "Control media playback (play, pause, stop, seek, status)",
        {
            "action": {"type": "string", "enum": ["play", "pause", "stop", "seek", "status"]},
            "target": {"type": "string", "enum": ["sagetv", "channels"], "default": "sagetv"},
            "payload": {"type": "object", "description": "Action-specific parameters"},
        },
    ),
    _tool_def(
        "metadata",
        "Retrieve program metadata by ID",
        {
            "target": {"type": "string", "enum": ["sagetv", "channels"]},
            "program_id": {"type": "string"},
        },
    ),
    _tool_def(
        "search",
        "Search for programs across backends",
        {
            "query": {"type": "string"},
            "target": {"type": "string", "description": "Optional backend filter"},
        },
    ),
    _tool_def(
        "system",
        "Run system commands (info, diagnostics, volume, reboot, shutdown)",
        {
            "action": {"type": "string", "enum": ["info", "diagnostics", "volume", "reboot", "shutdown"]},
            "payload": {"type": "object"},
        },
    ),
]


# ------------------------------------------------------------------
# MCP Server
# ------------------------------------------------------------------

class MCPServer:
    """
    Async MCP server that listens on a TCP port and dispatches
    JSON-RPC–style requests to the orchestrator.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._orchestrator: Any = None
        self._server: Optional[asyncio.AbstractServer] = None

    def bind_orchestrator(self, orchestrator: Any) -> None:
        """Attach the orchestrator instance for request routing."""
        self._orchestrator = orchestrator

    @property
    def tools(self) -> List[Dict[str, Any]]:
        """Return the list of advertised MCP tools."""
        return list(MCP_TOOLS)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the MCP TCP server."""
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port,
        )
        logger.info("MCP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Stop the MCP TCP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("MCP server stopped")

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single MCP client connection."""
        addr = writer.get_extra_info("peername")
        logger.info("MCP connection from %s", addr)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    request = json.loads(raw.decode())
                except json.JSONDecodeError:
                    response = {"error": "Invalid JSON"}
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
                    continue

                response = await self._dispatch(request)
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("MCP connection error from %s", addr)
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info("MCP connection closed: %s", addr)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Route an MCP request to the correct orchestrator method."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        logger.info("MCP dispatch: method=%s id=%s", method, req_id)

        if self._orchestrator is None:
            return self._error_response(req_id, "Orchestrator not bound")

        try:
            if method == "tools/list":
                return self._success_response(req_id, {"tools": self.tools})

            if method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {})
                result = await self._call_tool(tool_name, tool_args)
                return self._success_response(req_id, result)

            return self._error_response(req_id, f"Unknown method '{method}'")
        except Exception as exc:
            logger.exception("MCP dispatch error")
            return self._error_response(req_id, str(exc))

    async def _call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an MCP tool call via the orchestrator."""
        orch = self._orchestrator

        if name == "query":
            return await orch.run_query(
                args.get("prompt", ""),
                synthesize=args.get("synthesize", True),
            )
        if name == "playback":
            return await orch.run_playback(
                args.get("action", "status"),
                args.get("target", "sagetv"),
                args.get("payload", {}),
            )
        if name == "metadata":
            return await orch.run_metadata(
                args.get("target", "sagetv"),
                args.get("program_id", ""),
            )
        if name == "search":
            return await orch.run_search(
                args.get("query", ""),
                target=args.get("target"),
            )
        if name == "system":
            return await orch.run_system(
                args.get("action", "info"),
                args.get("payload", {}),
            )

        return {"error": f"Unknown tool '{name}'"}

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _success_response(req_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error_response(req_id: Any, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"message": message}}
