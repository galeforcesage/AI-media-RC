"""
system.py
FastAPI routes for system-level operations.
"""

from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from services.system import SystemService

router = APIRouter(prefix="/system", tags=["system"])
service: SystemService | None = None


def init_router(system_service: SystemService) -> None:
    """Bind the system service at startup."""
    global service
    service = system_service


def _require_service() -> SystemService:
    if service is None:
        raise HTTPException(status_code=500, detail="System service not initialized")
    return service


class VolumeRequest(BaseModel):
    level: int
    muted: Optional[bool] = None


@router.get("/info")
async def system_info():
    """Return system information (OS, CPU, memory, hostname)."""
    svc = _require_service()
    result = await svc.execute("info", {})
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return {
        "os": result.get("os", "unknown"),
        "hostname": result.get("hostname"),
        "cpu": result.get("cpu"),
        "memory": result.get("memory"),
    }


@router.get("/volume")
async def get_volume():
    """Return current volume state."""
    svc = _require_service()
    result = await svc.execute("volume", {})
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return {
        "level": result.get("level", 0),
        "muted": result.get("muted", False),
    }


@router.post("/volume")
async def set_volume(request: VolumeRequest):
    """Set the system volume level."""
    svc = _require_service()
    payload = {"level": request.level}
    if request.muted is not None:
        payload["muted"] = request.muted
    result = await svc.execute("volume", payload)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@router.post("/reboot")
async def reboot():
    """Initiate a system reboot."""
    svc = _require_service()
    result = await svc.execute("reboot", {})
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result
