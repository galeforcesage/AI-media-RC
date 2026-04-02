"""
playback.py
FastAPI routes for playback control.
"""

from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional

from services.playback_controller import PlaybackController

router = APIRouter(prefix="/playback", tags=["playback"])
controller: PlaybackController | None = None


def init_router(playback_controller: PlaybackController) -> None:
    """Bind the playback controller at startup."""
    global controller
    controller = playback_controller


def _require_controller() -> PlaybackController:
    if controller is None:
        raise HTTPException(status_code=500, detail="Playback controller not initialized")
    return controller


class PlayRequest(BaseModel):
    target: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


class SeekRequest(BaseModel):
    target: Optional[str] = None
    position: int


@router.get("/nowplaying")
async def now_playing(target: str | None = None):
    ctrl = _require_controller()
    states = await ctrl.now_playing(target)
    return {k: v.to_dict() for k, v in states.items()}


@router.post("/play")
async def play(request: PlayRequest):
    ctrl = _require_controller()
    return await ctrl.play(request.target, request.payload)


@router.post("/pause")
async def pause(target: str | None = None):
    ctrl = _require_controller()
    return await ctrl.pause(target)


@router.post("/seek")
async def seek(request: SeekRequest):
    ctrl = _require_controller()
    return await ctrl.seek(request.position, request.target)


@router.post("/stop")
async def stop(target: str | None = None):
    ctrl = _require_controller()
    return await ctrl.stop(target)
