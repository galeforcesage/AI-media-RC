"""
router.py
FastAPI router exposing core orchestrator command execution.
"""

from __future__ import annotations
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

router = APIRouter()
_orchestrator: Any = None


def bind_orchestrator(orchestrator: Any) -> None:
    """Inject the orchestrator at startup."""
    global _orchestrator
    _orchestrator = orchestrator


class ExecuteRequest(BaseModel):
    """Request body for /execute."""
    command: str
    payload: Optional[Dict[str, Any]] = None


@router.on_event("startup")
async def startup_event() -> None:
    if _orchestrator is None:
        # Lazy import for standalone server.py usage
        from orchestrator import Orchestrator
        orch = Orchestrator(config={})
        await orch.initialize()
        bind_orchestrator(orch)


@router.on_event("shutdown")
async def shutdown_event() -> None:
    if _orchestrator:
        await _orchestrator.shutdown()


@router.post("/execute")
async def execute_command(request: ExecuteRequest):
    """
    Execute a namespaced command via the orchestrator.

    Example:
    {
        "command": "sagetv.play",
        "payload": {"id": 123}
    }
    """
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")

    if not request.command:
        raise HTTPException(status_code=400, detail="Missing 'command' field")

    result = await _orchestrator.execute(request.command, request.payload)

    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return result


class QueryRequest(BaseModel):
    """Request body for /query."""
    text: str = ""
    prompt: str = ""
    systems: Optional[list[str]] = None
    system: Optional[str] = None  # Legacy single-system field
    synthesize: bool = False


class SearchRequest(BaseModel):
    """Request body for /search."""
    query: str
    target: Optional[str] = None


class PlaybackRequest(BaseModel):
    """Request body for /playback."""
    action: str
    system: Optional[str] = "sagetv"
    device_id: Optional[str] = None
    device: Optional[str] = None  # Bridge device name for Channels DVR
    position: Optional[float] = None
    level: Optional[int] = None
    seconds: Optional[float] = None


class SystemRequest(BaseModel):
    """Request body for /system."""
    action: str
    container: Optional[str] = None
    service: Optional[str] = None


@router.post("/query")
async def query(request: QueryRequest):
    """Natural-language query with LLM reasoning over transcripts + metadata."""
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    text = request.text or request.prompt
    if not text:
        raise HTTPException(status_code=400, detail="Missing 'text' or 'prompt' field")
    # Support both new multi-system and legacy single-system field
    systems = request.systems or ([request.system] if request.system else None)
    result = await _orchestrator.run_query(text, synthesize=request.synthesize, systems=systems)
    return {"response": result.get("llm_response", result.get("response", result.get("error", str(result))))}


@router.post("/search")
async def search(request: SearchRequest):
    """Search programs and transcripts."""
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")

    # Search transcripts for cross-metadata queries
    transcript_results = await _orchestrator.search.transcript_search(request.query)
    program_results = await _orchestrator.run_search(request.query, target=request.target)

    return {
        "programs": program_results,
        "transcripts": transcript_results,
    }


@router.post("/playback")
async def playback(request: PlaybackRequest):
    """Execute a playback action."""
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    payload = {}
    if request.position is not None:
        payload["position"] = request.position
    if request.level is not None:
        payload["level"] = request.level
    if request.seconds is not None:
        payload["seconds"] = request.seconds
    if request.device_id:
        payload["device_id"] = request.device_id
    if request.device:
        payload["device"] = request.device
    return await _orchestrator.run_playback(request.action, target=request.system or "sagetv", payload=payload)


@router.post("/system")
async def system(request: SystemRequest):
    """Execute a system command."""
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    payload = {}
    if request.container:
        payload["container"] = request.container
    if request.service:
        payload["service"] = request.service
    return await _orchestrator.run_system(request.action, payload=payload)


@router.get("/health")
async def health():
    """Health check."""
    return {"status": "ok"}


@router.get("/bridge/devices")
async def bridge_devices():
    """List connected Channels Bridge devices available for playback control."""
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    result = await _orchestrator.execute("channels.get_bridge_devices", {})
    if isinstance(result, dict) and result.get("success"):
        return {"devices": result.get("data", [])}
    return {"devices": [], "error": result.get("message", "Could not fetch bridge devices")}
