"""
server.py
HTTP server for the Unified Session Manager.

Uses aiohttp to expose REST endpoints for device management and session resolution.
"""

from __future__ import annotations
import json
import logging
from typing import Any, Dict

from aiohttp import web

from .api import SessionManagerAPI
from .registry import DeviceRegistry
from .resolver import SessionResolver

logger = logging.getLogger(__name__)


def create_app(config: Dict[str, Any]) -> web.Application:
    """Build and return the aiohttp Application."""

    registry = DeviceRegistry(
        db_path=config.get("db_path", "devices.db"),
        device_limit=config.get("device_limit", 15),
    )
    registry.open()

    resolver = SessionResolver(
        registry=registry,
        mcp_config={
            "sagetv_host": config.get("sagetv_mcp_host", "127.0.0.1"),
            "sagetv_port": config.get("sagetv_mcp_port", 8766),
            "channels_host": config.get("channels_mcp_host", "127.0.0.1"),
            "channels_port": config.get("channels_mcp_port", 8767),
        },
    )

    api = SessionManagerAPI(registry, resolver)

    # ------------------------------------------------------------------
    # Route wrappers
    # ------------------------------------------------------------------

    async def _json_handler(handler, request: web.Request) -> web.Response:
        if request.content_type == "application/json":
            try:
                body = await request.json()
            except json.JSONDecodeError:
                body = {}
        else:
            body = {}
        # Merge query params
        body.update(dict(request.query))
        # Merge path params
        body.update(dict(request.match_info))
        result = await handler(body)
        return web.json_response(result)

    def route(handler):
        async def wrapped(request: web.Request) -> web.Response:
            return await _json_handler(handler, request)
        return wrapped

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    app = web.Application()
    app.router.add_get("/health", route(api.health))

    # Device management
    app.router.add_get("/devices", route(api.list_devices))
    app.router.add_get("/devices/default", route(api.get_default))
    app.router.add_get("/devices/{device_id}", route(api.get_device))
    app.router.add_post("/devices", route(api.add_device))
    app.router.add_post("/devices/pair/qr", route(api.pair_qr))
    app.router.add_post("/devices/pair/api", route(api.pair_api))
    app.router.add_put("/devices/{device_id}", route(api.update_device))
    app.router.add_delete("/devices/{device_id}", route(api.delete_device))
    app.router.add_post("/devices/{device_id}/default", route(api.set_default))

    # Session resolution
    app.router.add_get("/sessions", route(api.list_sessions))
    app.router.add_get("/sessions/resolve", route(api.resolve_session))
    app.router.add_get("/sessions/resolve/{device_id}", route(api.resolve_session))

    async def on_shutdown(app):
        registry.close()

    app.on_shutdown.append(on_shutdown)

    return app
