"""
search.py
FastAPI routes for unified search.
"""

from __future__ import annotations
from fastapi import APIRouter, HTTPException, Query

from services.search import SearchService

router = APIRouter(prefix="/search", tags=["search"])
service: SearchService | None = None


def init_router(search_service: SearchService) -> None:
    """Bind the search service at startup."""
    global service
    service = search_service


def _require_service() -> SearchService:
    if service is None:
        raise HTTPException(status_code=500, detail="Search service not initialized")
    return service


@router.get("/")
async def search(
    q: str = Query(..., min_length=1, description="Search query"),
    target: str | None = Query(None, description="Backend target: sagetv, channels, or omit for all"),
):
    """
    Search for programs across SageTV and/or ChannelsDVR.
    """
    svc = _require_service()

    if target:
        return await svc.search_programs(target, q)

    return await svc.search_all(q)
