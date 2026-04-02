"""
server.py
FastAPI application wrapper for the Orchestrator backend.
Mounts the API router and sub-route modules.
Can be used standalone or via main.py.
"""

from __future__ import annotations
import uvicorn
from fastapi import FastAPI

from api.router import router as orchestrator_router
from api.routes.playback import router as playback_router
from api.routes.search import router as search_router
from api.routes.voice import router as voice_router
from api.routes.system import router as system_router


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.
    """
    app = FastAPI(
        title="LLM Remote Orchestrator",
        description="Unified control API for SageTV + ChannelsDVR + local AI",
        version="0.2.0",
    )

    # Core orchestrator route
    app.include_router(orchestrator_router, prefix="/orchestrator")

    # Sub-routes
    app.include_router(playback_router, prefix="/api")
    app.include_router(search_router, prefix="/api")
    app.include_router(voice_router, prefix="/api")
    app.include_router(system_router, prefix="/api")

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "api.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
