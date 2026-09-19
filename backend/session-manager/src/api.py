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

    async def discover_sagetv(self, body: Dict) -> Dict:
        """On-demand SageTV discovery — delegates to the reconciler."""
        return await self.reconcile_sagetv()

    async def reconcile_sagetv(self) -> Dict:
        """Reconcile SageTV context devices against currently-connected clients.

        - Live contexts are upserted and marked online (last_seen refreshed).
        - SageTV devices whose context is no longer live are marked offline.
        - Rows, friendly names, and default flags are preserved.
        If SageTV/MCP is unreachable, no offline sweep happens (avoids flapping).
        """
        ctx_ids = await self.resolver.fetch_sagetv_context_ids()
        if ctx_ids is None:
            return {"success": False, "error": "Could not reach SageTV MCP"}

        discovered = []
        online_device_ids = []
        for ctx_id in ctx_ids:
            device_id = f"sagetv-ctx-{ctx_id}"
            existing = self.registry.get_device(device_id)
            if existing:
                self.registry.set_online(device_id, True)
            else:
                device = Device(
                    device_id=device_id,
                    friendly_name=ctx_id,
                    system="sagetv",
                    platform="placeshifter",
                    capabilities={
                        "sagetv_context": ctx_id,
                        "supports_seek": True,
                        "supports_volume": True,
                        "supports_commercial_skip": True,
                    },
                    pairing_method="api",
                    online=True,
                )
                try:
                    self.registry.add_device(device)
                except ValueError as exc:
                    logger.warning("Could not register discovered device %s: %s", device_id, exc)
                    continue
            online_device_ids.append(device_id)
            d = self.registry.get_device(device_id)
            if d:
                discovered.append(d.to_dict())

        swept = self.registry.mark_offline_except("sagetv", online_device_ids)
        return {
            "success": True,
            "discovered": discovered,
            "count": len(discovered),
            "online": online_device_ids,
            "swept_offline": swept,
        }

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
