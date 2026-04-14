"""
Unified device manager for Channels DVR playback control.

Supports two connection modes:

1. **Bridge** (Android TV / Fire TV):
   Android blocks inbound connections, so the Channels Bridge APK connects
   *outbound* to this server over WebSocket.  Commands are proxied by the
   APK to localhost:57000 on the device.

2. **Direct** (Apple TV):
   Apple TV allows inbound connections.  Devices are discovered via Bonjour
   (_channels_app._tcp) and commands go directly to device_ip:57000.

Both modes present a uniform ``send_command(method, path, body)`` interface
so the tool layer doesn't need to know the difference.

Bridge Protocol
---------------
APK → Server (on connect):
    {"type": "register", "token": "...", "device_name": "...", "device_model": "..."}

Server → APK (command):
    {"id": "<uuid>", "method": "GET|POST", "path": "/api/status", "body": null}

APK → Server (response):
    {"id": "<uuid>", "status": 200, "body": {...}}
"""

from __future__ import annotations
import asyncio
import json
import logging
import subprocess
import time
import uuid
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# How long to wait for a device to respond to a command
_COMMAND_TIMEOUT = 10.0

# How often to re-scan Bonjour for direct devices (seconds)
_DISCOVERY_INTERVAL = 60


# =====================================================================
# Abstract device interface
# =====================================================================

class ChannelsDevice:
    """Base class for any device that can receive Channels App API commands."""

    def __init__(self, device_name: str, device_model: str, device_type: str):
        self.device_name = device_name
        self.device_model = device_model
        self.device_type = device_type  # "bridge" or "direct"
        self.connected_at = time.time()

    async def send_command(
        self, method: str, path: str, body: Any = None
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def info(self) -> Dict[str, Any]:
        return {
            "device_name": self.device_name,
            "device_model": self.device_model,
            "device_type": self.device_type,
            "connected_at": self.connected_at,
        }


# =====================================================================
# Bridge device — Android TV / Fire TV (WebSocket reverse-connect)
# =====================================================================

class BridgeDevice(ChannelsDevice):
    """Connected Android TV device running the Channels Bridge APK."""

    def __init__(self, ws: web.WebSocketResponse, device_name: str, device_model: str):
        super().__init__(device_name, device_model, "bridge")
        self.ws = ws
        self._pending: Dict[str, asyncio.Future] = {}

    def handle_message(self, data: str) -> None:
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        msg_id = msg.get("id")
        if msg_id and msg_id in self._pending:
            if not self._pending[msg_id].done():
                self._pending[msg_id].set_result(msg)

    async def send_command(
        self, method: str, path: str, body: Any = None
    ) -> Dict[str, Any]:
        cmd_id = str(uuid.uuid4())
        cmd = {"id": cmd_id, "method": method, "path": path, "body": body}
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            await self.ws.send_str(json.dumps(cmd))
            return await asyncio.wait_for(fut, timeout=_COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            return {"status": 504, "body": {"error": "Device did not respond in time"}}
        except ConnectionError:
            return {"status": 503, "body": {"error": "Device disconnected"}}
        finally:
            self._pending.pop(cmd_id, None)

    def resolve_pending_with_error(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"status": 503, "body": {"error": "Device disconnected"}})
        self._pending.clear()


# =====================================================================
# Direct device — Apple TV (HTTP to device:57000)
# =====================================================================

class DirectDevice(ChannelsDevice):
    """Apple TV or other device reachable directly on port 57000."""

    def __init__(self, device_name: str, ip: str, port: int = 57000):
        super().__init__(device_name, device_model="Apple TV", device_type="direct")
        self.ip = ip
        self.port = port
        self._base_url = f"http://{ip}:{port}"
        self._session: Optional[aiohttp.ClientSession] = None

    def info(self) -> Dict[str, Any]:
        d = super().info()
        d["ip"] = self.ip
        d["port"] = self.port
        return d

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_COMMAND_TIMEOUT),
            )
        return self._session

    async def send_command(
        self, method: str, path: str, body: Any = None
    ) -> Dict[str, Any]:
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        try:
            if method.upper() == "GET":
                async with session.get(url) as resp:
                    return await self._parse_response(resp)
            else:
                async with session.post(url, json=body) as resp:
                    return await self._parse_response(resp)
        except asyncio.TimeoutError:
            return {"status": 504, "body": {"error": f"Device {self.device_name} did not respond in time"}}
        except aiohttp.ClientConnectorError:
            return {"status": 503, "body": {"error": f"Cannot reach {self.device_name} at {self._base_url} — is Channels open?"}}
        except Exception as e:
            return {"status": 502, "body": {"error": str(e)}}

    @staticmethod
    async def _parse_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        status = resp.status
        ct = resp.content_type or ""
        if "json" in ct:
            body = await resp.json()
        else:
            text = await resp.text()
            try:
                body = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                body = {"raw": text}
        return {"status": status, "body": body}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# =====================================================================
# Bonjour discovery — finds Apple TV / direct devices
# =====================================================================

async def _discover_bonjour() -> list[Dict[str, Any]]:
    """
    Run avahi-browse to find Channels app instances on the LAN.

    Returns list of dicts: {"name": ..., "ip": ..., "port": ..., "hostname": ...}
    Only includes non-Android devices (Apple TV, etc.).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "avahi-browse", "-tpr", "_channels_app._tcp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("Bonjour discovery unavailable: %s", e)
        return []

    results = []
    for line in stdout.decode(errors="replace").splitlines():
        if not line.startswith("="):
            continue
        # Format: =;iface;proto;name;service;domain;hostname;ip;port;txt
        parts = line.split(";")
        if len(parts) < 9:
            continue
        name = parts[3]
        hostname = parts[6]
        ip = parts[7]
        port = int(parts[8]) if parts[8].isdigit() else 57000

        # Skip Android devices — they need the bridge, not direct connect.
        # Android hostnames are typically "Android-N.local"
        hostname_lower = hostname.lower()
        if "android" in hostname_lower:
            continue

        results.append({"name": name, "ip": ip, "port": port, "hostname": hostname})

    return results


# =====================================================================
# Unified device manager
# =====================================================================

class BridgeManager:
    """
    Manages all Channels playback devices — both bridge (Android TV)
    and direct (Apple TV) connections.
    """

    def __init__(self, auth_token: str = ""):
        self._auth_token = auth_token
        self._devices: Dict[str, ChannelsDevice] = {}
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._discovery_task: Optional[asyncio.Task] = None

    @property
    def connected_devices(self) -> Dict[str, Dict]:
        return {name: dev.info() for name, dev in self._devices.items()}

    def get_device(self, device_name: str = "") -> Optional[ChannelsDevice]:
        if device_name and device_name in self._devices:
            return self._devices[device_name]
        if not device_name and self._devices:
            return next(iter(self._devices.values()))
        return None

    async def start(self, host: str, port: int) -> None:
        # Start WebSocket server for bridge APK connections
        self._app = web.Application()
        self._app.router.add_get("/bridge", self._ws_handler)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        logger.info("Bridge WebSocket listener on %s:%d", host, port)

        # Start periodic Bonjour discovery for direct devices (Apple TV)
        self._discovery_task = asyncio.ensure_future(self._discovery_loop())

    async def stop(self) -> None:
        if self._discovery_task:
            self._discovery_task.cancel()
        for dev in list(self._devices.values()):
            if isinstance(dev, BridgeDevice):
                dev.resolve_pending_with_error()
                await dev.ws.close()
            elif isinstance(dev, DirectDevice):
                await dev.close()
        self._devices.clear()
        if self._runner:
            await self._runner.cleanup()

    # -----------------------------------------------------------------
    # Bonjour discovery loop for Apple TV / direct devices
    # -----------------------------------------------------------------

    async def _discovery_loop(self) -> None:
        """Periodically scan for Bonjour-advertised Channels apps."""
        while True:
            try:
                await self._run_discovery()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Bonjour discovery error")
            await asyncio.sleep(_DISCOVERY_INTERVAL)

    async def _run_discovery(self) -> None:
        found = await _discover_bonjour()
        found_names = set()

        for entry in found:
            name = entry["name"]
            ip = entry["ip"]
            port = entry["port"]
            found_names.add(name)

            existing = self._devices.get(name)
            if isinstance(existing, DirectDevice):
                # Update IP if changed
                if existing.ip != ip or existing.port != port:
                    await existing.close()
                    self._devices[name] = DirectDevice(name, ip, port)
                    logger.info("Direct device updated: %s -> %s:%d", name, ip, port)
            elif existing is None:
                # New direct device
                self._devices[name] = DirectDevice(name, ip, port)
                logger.info("Direct device discovered: %s at %s:%d", name, ip, port)
            # If name collides with a bridge device, bridge takes priority

        # Remove stale direct devices that are no longer advertised
        for name in list(self._devices):
            dev = self._devices[name]
            if isinstance(dev, DirectDevice) and name not in found_names:
                await dev.close()
                del self._devices[name]
                logger.info("Direct device gone: %s", name)

    # -----------------------------------------------------------------
    # WebSocket handler for bridge APK connections (Android TV)
    # -----------------------------------------------------------------

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        peer = request.remote
        logger.info("Bridge WebSocket connection from %s", peer)

        device_name = None
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=10.0)
            if msg.type != aiohttp.WSMsgType.TEXT:
                await ws.close()
                return ws

            reg = json.loads(msg.data)
            if reg.get("type") != "register":
                logger.warning("Bridge: expected register, got %s", reg.get("type"))
                await ws.close()
                return ws

            if self._auth_token and reg.get("token") != self._auth_token:
                logger.warning("Bridge: invalid token from %s", peer)
                await ws.send_str(json.dumps({"type": "error", "message": "Invalid auth token"}))
                await ws.close()
                return ws

            device_name = reg.get("device_name", f"device-{peer}")
            device_model = reg.get("device_model", "unknown")

            # Remove old connection (bridge or direct) for same name
            old = self._devices.get(device_name)
            if isinstance(old, BridgeDevice):
                old.resolve_pending_with_error()
                await old.ws.close()
            elif isinstance(old, DirectDevice):
                await old.close()

            device = BridgeDevice(ws, device_name, device_model)
            self._devices[device_name] = device

            await ws.send_str(json.dumps({"type": "registered", "device_name": device_name}))
            logger.info("Bridge device registered: %s (%s)", device_name, device_model)

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    device.handle_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break

        except (asyncio.TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            logger.warning("Bridge connection error from %s: %s", peer, e)
        finally:
            if device_name and device_name in self._devices:
                dev = self._devices[device_name]
                if isinstance(dev, BridgeDevice):
                    dev.resolve_pending_with_error()
                del self._devices[device_name]
                logger.info("Bridge device disconnected: %s", device_name)

        return ws
