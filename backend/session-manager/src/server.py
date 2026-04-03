"""
server.py
HTTP server for the Unified Session Manager.

Uses aiohttp to expose REST endpoints for device management,
session resolution, and authentication.
"""

from __future__ import annotations
import json
import logging
from typing import Any, Dict

from aiohttp import web

from .api import SessionManagerAPI
from .auth import AuthManager, APP_COOKIE, ADMIN_COOKIE, APP_MAX_AGE
from .registry import DeviceRegistry
from .resolver import SessionResolver

logger = logging.getLogger(__name__)


# Paths that don't require app auth
_PUBLIC_PATHS = {"/health", "/auth/login", "/auth/check"}


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
    auth = AuthManager(config_path=config.get("auth_config", "auth.json"))

    # ------------------------------------------------------------------
    # Middleware: CORS + App-level auth
    # ------------------------------------------------------------------

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            resp = await handler(request)
        origin = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        """Check app_session cookie on non-public paths."""
        path = request.path
        if request.method == "OPTIONS":
            return await handler(request)
        if path in _PUBLIC_PATHS or path.startswith("/auth/"):
            return await handler(request)
        # Internal requests (from orchestrator on localhost) are trusted
        peer = request.remote or ""
        if peer in ("127.0.0.1", "::1"):
            return await handler(request)
        # Check app cookie
        token = request.cookies.get(APP_COOKIE, "")
        if not auth.validate_app_token(token):
            return web.json_response(
                {"success": False, "error": "unauthorized", "message": "Login required"},
                status=401,
            )
        return await handler(request)

    # ------------------------------------------------------------------
    # Route wrappers
    # ------------------------------------------------------------------

    async def _json_handler(handler_fn, request: web.Request) -> web.Response:
        if request.content_type == "application/json":
            try:
                body = await request.json()
            except json.JSONDecodeError:
                body = {}
        else:
            body = {}
        body.update(dict(request.query))
        body.update(dict(request.match_info))
        result = await handler_fn(body)
        return web.json_response(result)

    def route(handler_fn):
        async def wrapped(request: web.Request) -> web.Response:
            return await _json_handler(handler_fn, request)
        return wrapped

    # ------------------------------------------------------------------
    # Auth routes
    # ------------------------------------------------------------------

    async def auth_login(request: web.Request) -> web.Response:
        """App-level login. Sets a 2-week HTTP-only cookie."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            body = {}
        password = body.get("password", "")
        if not auth.verify_app_password(password):
            return web.json_response(
                {"success": False, "error": "invalid_password"},
                status=401,
            )
        token = auth.create_app_token()
        resp = web.json_response({"success": True})
        resp.set_cookie(
            APP_COOKIE, token,
            max_age=APP_MAX_AGE,
            httponly=True,
            samesite="Strict",
            secure=True,
            path="/",
        )
        return resp

    async def auth_check(request: web.Request) -> web.Response:
        """Check if the current app session is valid."""
        token = request.cookies.get(APP_COOKIE, "")
        valid = auth.validate_app_token(token)
        return web.json_response({"authenticated": valid})

    async def auth_logout(request: web.Request) -> web.Response:
        """Clear app session cookie."""
        resp = web.json_response({"success": True})
        resp.del_cookie(APP_COOKIE, path="/")
        return resp

    async def admin_login(request: web.Request) -> web.Response:
        """Admin login. Sets a session-only HTTP-only cookie (no max-age)."""
        # Require valid app session first
        app_token = request.cookies.get(APP_COOKIE, "")
        if not auth.validate_app_token(app_token):
            return web.json_response(
                {"success": False, "error": "app_auth_required"},
                status=401,
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            body = {}
        username = body.get("username", "")
        password = body.get("password", "")
        if not auth.verify_admin(username, password):
            return web.json_response(
                {"success": False, "error": "invalid_credentials"},
                status=401,
            )
        token = auth.create_admin_token(username)
        resp = web.json_response({"success": True, "username": username})
        resp.set_cookie(
            ADMIN_COOKIE, token,
            httponly=True,
            samesite="Strict",
            secure=True,
            path="/",
            # No max_age → session cookie, expires when browser closes
        )
        return resp

    async def admin_check(request: web.Request) -> web.Response:
        """Validate admin session token. Returns username if valid."""
        token = request.cookies.get(ADMIN_COOKIE, "")
        username = auth.validate_admin_token(token)
        return web.json_response({
            "authenticated": username is not None,
            "username": username,
        })

    async def admin_logout(request: web.Request) -> web.Response:
        """Revoke admin session and clear cookie."""
        token = request.cookies.get(ADMIN_COOKIE, "")
        if token:
            auth.revoke_admin_token(token)
        resp = web.json_response({"success": True})
        resp.del_cookie(ADMIN_COOKIE, path="/")
        return resp

    # ------------------------------------------------------------------
    # Whoami
    # ------------------------------------------------------------------

    async def whoami(request: web.Request) -> web.Response:
        remote = request.remote or ""
        forwarded = request.headers.get("X-Forwarded-For", "")
        client_ip = forwarded.split(",")[0].strip() if forwarded else remote
        return web.json_response({"ip": client_ip})

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    app.router.add_route("OPTIONS", "/{path_info:.*}", lambda r: web.Response(status=204))

    # Auth
    app.router.add_post("/auth/login", auth_login)
    app.router.add_get("/auth/check", auth_check)
    app.router.add_post("/auth/logout", auth_logout)
    app.router.add_post("/auth/admin/login", admin_login)
    app.router.add_get("/auth/admin/check", admin_check)
    app.router.add_post("/auth/admin/logout", admin_logout)

    # Health & info
    app.router.add_get("/health", route(api.health))
    app.router.add_get("/whoami", whoami)

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

    async def on_shutdown(app_inst):
        registry.close()

    app.on_shutdown.append(on_shutdown)

    return app
