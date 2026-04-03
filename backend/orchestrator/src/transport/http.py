"""
http.py
FastAPI HTTP transport layer for the orchestrator.
Exposes REST endpoints for query, playback, metadata, system, and search.
All endpoints accept/return JSON and route through the orchestrator.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# The orchestrator instance is injected at startup.
_orchestrator: Any = None


def init_http_transport(orchestrator: Any) -> None:
    """Bind the orchestrator instance used by all route handlers."""
    global _orchestrator
    _orchestrator = orchestrator
    logger.info("HTTP transport initialized")


def _require_orchestrator() -> Any:
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return _orchestrator


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------

class QueryRequest(BaseModel):
    """Request body for /query."""
    prompt: str
    synthesize: bool = True
    metadata: Optional[Dict[str, Any]] = None


class PlaybackRequest(BaseModel):
    """Request body for /playback."""
    action: str  # play, pause, stop, seek, status
    target: str = "sagetv"
    device_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


class MetadataRequest(BaseModel):
    """Request body for /metadata."""
    target: str = "sagetv"
    program_id: Optional[str] = None
    query: Optional[str] = None


class SystemRequest(BaseModel):
    """Request body for /system."""
    action: str  # info, diagnostics, volume, reboot, shutdown
    payload: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------
# Centralized error wrapper
# ------------------------------------------------------------------

async def _safe_execute(coro):
    """Await a coroutine and convert errors to HTTPException."""
    try:
        result = await coro
        if isinstance(result, dict) and "error" in result:
            raise HTTPException(status_code=422, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unhandled transport error")
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.post("/query")
async def query(request: QueryRequest):
    """
    Run a text or voice query through the LLM pipeline.
    Returns the LLM response and optionally a TTS audio path.
    """
    orch = _require_orchestrator()
    return await _safe_execute(
        orch.run_query(request.prompt, synthesize=request.synthesize, metadata=request.metadata)
    )


@router.post("/query/voice")
async def query_voice(audio: UploadFile = File(...)):
    """
    Run a voice query: upload audio → transcription → LLM → TTS.
    """
    import tempfile, os
    orch = _require_orchestrator()

    suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await audio.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await orch.run_query_voice(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.post("/playback")
async def playback(request: PlaybackRequest):
    """
    Control playback: play, pause, stop, seek, or query status.
    If device_id is provided, the orchestrator resolves it to a session_id.
    """
    orch = _require_orchestrator()
    payload = dict(request.payload or {})
    if request.device_id:
        payload["device_id"] = request.device_id
    return await _safe_execute(
        orch.run_playback(request.action, request.target, payload)
    )


@router.post("/metadata")
async def metadata(request: MetadataRequest):
    """
    Fetch program metadata or search for programs.
    """
    orch = _require_orchestrator()
    if request.query:
        return await _safe_execute(
            orch.run_search(request.query, target=request.target)
        )
    if request.program_id:
        return await _safe_execute(
            orch.run_metadata(request.target, request.program_id)
        )
    raise HTTPException(status_code=400, detail="Provide 'program_id' or 'query'")


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, description="Search query"),
    target: str | None = Query(None, description="Backend to search"),
):
    """Search for programs across backends."""
    orch = _require_orchestrator()
    return await _safe_execute(orch.run_search(q, target=target))


@router.post("/system")
async def system(request: SystemRequest):
    """
    Run system commands: info, diagnostics, volume, reboot, shutdown.
    """
    orch = _require_orchestrator()
    return await _safe_execute(
        orch.run_system(request.action, request.payload or {})
    )


@router.get("/health")
async def health():
    """Basic health check."""
    return {"status": "ok"}


@router.get("/services")
async def services():
    """
    Health-check all AI-media-RC services.
    Returns a dict of service_id → { name, port, status, latency_ms }.
    """
    import aiohttp
    import time

    checks = {
        "orchestrator": {"name": "Orchestrator", "port": 8000, "url": "http://127.0.0.1:8000/api/health"},
        "mcp_sagetv":   {"name": "MCP SageTV",   "port": 8766, "url": None},
        "mcp_channels": {"name": "MCP Channels",  "port": 8767, "url": None},
        "mcp_linux":    {"name": "MCP Linux",     "port": 8768, "url": None},
        "session_mgr":  {"name": "Session Manager","port": 8769, "url": "http://127.0.0.1:8769/health"},
        "transcription":{"name": "Transcription",  "port": 8770, "url": None},
    }

    results = {}
    timeout = aiohttp.ClientTimeout(total=3)

    async def check_http(sid, info):
        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(info["url"]) as resp:
                    ms = round((time.monotonic() - t0) * 1000)
                    results[sid] = {
                        "name": info["name"],
                        "port": info["port"],
                        "status": "up" if resp.status == 200 else "degraded",
                        "latency_ms": ms,
                    }
        except Exception:
            ms = round((time.monotonic() - t0) * 1000)
            results[sid] = {"name": info["name"], "port": info["port"], "status": "down", "latency_ms": ms}

    async def check_tcp(sid, info):
        """TCP connect check for MCP servers (JSON-RPC, no HTTP health route)."""
        import asyncio
        t0 = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", info["port"]), timeout=2
            )
            writer.close()
            await writer.wait_closed()
            ms = round((time.monotonic() - t0) * 1000)
            results[sid] = {"name": info["name"], "port": info["port"], "status": "up", "latency_ms": ms}
        except Exception:
            ms = round((time.monotonic() - t0) * 1000)
            results[sid] = {"name": info["name"], "port": info["port"], "status": "down", "latency_ms": ms}

    import asyncio
    tasks = []
    for sid, info in checks.items():
        if sid == "orchestrator":
            # We're obviously up if serving this request
            results[sid] = {"name": info["name"], "port": info["port"], "status": "up", "latency_ms": 0}
            continue
        if info["url"]:
            tasks.append(check_http(sid, info))
        else:
            tasks.append(check_tcp(sid, info))

    await asyncio.gather(*tasks)
    return {"services": results}
