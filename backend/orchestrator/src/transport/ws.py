"""
ws.py
WebSocket transport for real-time streaming.

Provides:
- /ws/events: Push-based state updates (replaces polling)
- /ws/query: Full-duplex query with token streaming and tool call visibility
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Any, Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()

# Global set of connected event listeners
_event_clients: Set[WebSocket] = set()
_orchestrator: Any = None


def init_ws_transport(orchestrator: Any) -> None:
    """Bind the orchestrator instance."""
    global _orchestrator
    _orchestrator = orchestrator


async def broadcast_event(event_type: str, data: Dict[str, Any]) -> None:
    """Broadcast an event to all connected WebSocket clients."""
    if not _event_clients:
        return
    message = json.dumps({"type": event_type, "data": data, "ts": time.time()})
    disconnected = set()
    for ws in _event_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    _event_clients.difference_update(disconnected)


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    """
    Push-based event stream for UI state updates.

    Server pushes:
      {"type": "playback_update", "data": {...}, "ts": 1234567890.0}
      {"type": "health", "data": {"status": "ok"}, "ts": ...}
      {"type": "transcription_job", "data": {...}, "ts": ...}

    Client can send:
      {"action": "ping"}  → server responds {"type": "pong"}
      {"action": "subscribe", "events": ["playback", "health"]}
    """
    await ws.accept()
    _event_clients.add(ws)
    logger.info("WebSocket events client connected (%d total)", len(_event_clients))

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.receive":
                text = msg.get("text", "")
                if text:
                    try:
                        data = json.loads(text)
                        if data.get("action") == "ping":
                            await ws.send_json({"type": "pong", "ts": time.time()})
                    except (json.JSONDecodeError, ValueError):
                        pass
            elif msg["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        _event_clients.discard(ws)
        logger.info("WebSocket events client disconnected (%d remaining)", len(_event_clients))


@router.websocket("/ws/query")
async def ws_query(ws: WebSocket):
    """
    Full-duplex query WebSocket for streaming LLM responses.

    Client sends:
      {"prompt": "...", "systems": ["sagetv", "channelsdvr"]}

    Server streams back:
      {"type": "status", "message": "Thinking"}
      {"type": "token", "text": "..."}
      {"type": "tool_call", "tool": "...", "status": "running"}
      {"type": "tool_call", "tool": "...", "status": "done", "duration_ms": 123}
      {"type": "done", "response": "...", "model": "...", "iterations": 2}
      {"type": "error", "message": "..."}
    """
    await ws.accept()
    logger.info("WebSocket query client connected")

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg["type"] != "websocket.receive":
                continue
            text = msg.get("text", "")
            if not text:
                continue

            try:
                request = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            prompt = request.get("prompt", "").strip()
            if not prompt:
                await ws.send_json({"type": "error", "message": "Empty prompt"})
                continue

            systems = request.get("systems")

            # Execute query with streaming callbacks
            async def status_cb(msg: str) -> None:
                try:
                    await ws.send_json({"type": "status", "message": msg})
                except Exception:
                    pass

            async def token_cb(token: str) -> None:
                try:
                    await ws.send_json({"type": "token", "text": token})
                except Exception:
                    pass

            try:
                if _orchestrator is None:
                    await ws.send_json({"type": "error", "message": "Orchestrator not ready"})
                    continue

                result = await _orchestrator.run_query(
                    prompt,
                    synthesize=False,
                    systems=systems,
                    status_callback=status_cb,
                    token_callback=token_cb,
                )

                if isinstance(result, dict) and "error" in result:
                    await ws.send_json({"type": "error", "message": result["error"]})
                else:
                    await ws.send_json({
                        "type": "done",
                        "response": result.get("response", ""),
                        "model": result.get("model", ""),
                        "iterations": result.get("iterations", 0),
                    })
            except Exception as exc:
                logger.exception("WebSocket query error")
                try:
                    await ws.send_json({"type": "error", "message": str(exc)})
                except Exception:
                    pass

    except WebSocketDisconnect:
        pass

    logger.info("WebSocket query client disconnected")
