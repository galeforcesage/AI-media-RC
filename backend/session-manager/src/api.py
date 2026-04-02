"""
api.py
HTTP API for the Unified Session Manager.

Provides REST endpoints for device management, session resolution,
and playback context. Used by the orchestrator and HTML5 frontend.
"""

from __future__ import annotations
import json
import logging
from typing import Any, Dict

from .models import Device
from .registry import DeviceRegistry
from .resolver import SessionResolver

logger = logging.getLogger(__name__)


class SessionManagerAPI:
    """HTTP request handler using a simple async dispatch table.

    This is framework-agnostic — the server (aiohttp) maps routes to these methods.
    Each method takes a parsed JSON body and returns a dict response.
    """

    def __init__(self, registry: DeviceRegistry, resolver: SessionResolver):
        self.registry = registry
        self.resolver = resolver

    # ------------------------------------------------------------------
    # Device endpoints
    # ------------------------------------------------------------------

    async def list_devices(self, body: Dict) -> Dict:
        include_expired = body.get("include_expired", False)
        devices = self.registry.list_devices(include_expired=include_expired)
        return {
            "success": True,
            "devices": [d.to_dict() for d in devices],
            "count": len(devices),
        }

    async def get_device(self, body: Dict) -> Dict:
        device_id = body.get("device_id", "")
        device = self.registry.get_device(device_id)
        if not device:
            return {"success": False, "error": f"Device '{device_id}' not found"}
        return {"success": True, "device": device.to_dict()}

    async def add_device(self, body: Dict) -> Dict:
        try:
            system = body["system"]
            name = body["friendly_name"]
            ip = body.get("ip_address", "")
            platform = body.get("platform", "unknown")
            device = self.registry.pair_manual(system, name, ip, platform)
            return {"success": True, "device": device.to_dict()}
        except KeyError as e:
            return {"success": False, "error": f"Missing field: {e}"}
        except ValueError as e:
            return {"success": False, "error": str(e)}

    async def pair_qr(self, body: Dict) -> Dict:
        try:
            device = self.registry.pair_from_qr(
                system=body["system"],
                device_id=body["device_id"],
                ip=body["ip"],
                name=body["name"],
            )
            return {"success": True, "device": device.to_dict()}
        except (KeyError, ValueError) as e:
            return {"success": False, "error": str(e)}

    async def pair_api(self, body: Dict) -> Dict:
        try:
            device = self.registry.pair_from_api(
                system=body["system"],
                client_info=body["client"],
            )
            return {"success": True, "device": device.to_dict()}
        except (KeyError, ValueError) as e:
            return {"success": False, "error": str(e)}

    async def update_device(self, body: Dict) -> Dict:
        device_id = body.get("device_id", "")
        updates = body.get("updates", {})
        device = self.registry.update_device(device_id, updates)
        if not device:
            return {"success": False, "error": f"Device '{device_id}' not found"}
        return {"success": True, "device": device.to_dict()}

    async def delete_device(self, body: Dict) -> Dict:
        device_id = body.get("device_id", "")
        deleted = self.registry.delete_device(device_id)
        return {"success": deleted, "device_id": device_id}

    async def set_default(self, body: Dict) -> Dict:
        device_id = body.get("device_id", "")
        self.registry.set_default(device_id)
        return {"success": True, "default_device_id": device_id}

    async def get_default(self, body: Dict) -> Dict:
        device = self.registry.get_default()
        if not device:
            return {"success": False, "error": "No default device set"}
        return {"success": True, "device": device.to_dict()}

    # ------------------------------------------------------------------
    # Session endpoints
    # ------------------------------------------------------------------

    async def resolve_session(self, body: Dict) -> Dict:
        device_id = body.get("device_id", "")
        if device_id:
            ctx = await self.resolver.resolve(device_id)
        else:
            ctx = await self.resolver.resolve_default()
        return {"success": ctx.error is None, **ctx.to_dict()}

    async def list_sessions(self, body: Dict) -> Dict:
        sessions = await self.resolver.list_active_sessions()
        return {"success": True, "sessions": sessions, "count": len(sessions)}

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self, body: Dict) -> Dict:
        count = len(self.registry.list_devices(include_expired=True))
        default = self.registry.get_default()
        return {
            "status": "ok",
            "devices": count,
            "default_device": default.device_id if default else None,
        }
