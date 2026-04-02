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
