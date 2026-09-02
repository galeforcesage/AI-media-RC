"""
gpu_router.py
Client + session-scoped lease manager for the GPU Resource Broker.

The broker (default http://10.0.0.10:7235) hands out short GPU leases so
this app shares the single GPU with paperless-ai and the live VSR / whisper
path instead of pinning Ollama directly.

Everything here FAILS OPEN: any broker error, timeout, or denial resolves to
the app's static local Ollama endpoint/model, so the assistant never stops
working when the broker is down.

Lease model (a "timeslot", not per-question):
  - On the first question we acquire a lease and keep it warm.
  - A background keepalive renews it before the broker's TTL (300s) expires,
    so a single long streaming answer (up to 600s) stays valid.
  - After each answer an idle timer starts; if no new question arrives within
    ``idle_release_seconds`` (default 120s) the lease is released so other
    apps get the GPU back.  A new question inside the window reuses and
    extends the warm lease.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)

# ── Defaults (overridable via config["gpu_router"] or GPU_ROUTER_* env) ──
DEFAULT_URL = "http://10.0.0.10:7235"
DEFAULT_APPLICATION_ID = "sagetv-ai-remote"

DEFAULT_PROVIDER_ENDPOINTS: Dict[str, str] = {
    "sagetv-cl-ollama": "http://10.0.0.10:11434",
    "predator-tc-ollama": "http://10.0.0.11:11434",
}

# Registered modes, richest -> leanest.  ``model`` is the Ollama tag to run.
DEFAULT_MODES: List[Dict[str, Any]] = [
    {"name": "qwen3-14b", "model": "qwen3:14b", "quality_rank": 3, "vram_mb": 11000},
    {"name": "qwen25-14b", "model": "qwen2.5:14b", "quality_rank": 2, "vram_mb": 10000},
    {"name": "hermes3-8b", "model": "hermes3:8b", "quality_rank": 1, "vram_mb": 6000},
]

DEFAULT_LEASE_TTL_SECONDS = 300
DEFAULT_IDLE_RELEASE_SECONDS = 120
DEFAULT_QUEUE_MAX_WAIT_MS = 4000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
# Renew when a lease is within this many seconds of expiring.
RENEW_MARGIN_SECONDS = 60


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _extract_generation(data: Optional[Dict[str, Any]], fallback: int) -> int:
    """Pull the (incremented) generation out of a renew/lease response,
    tolerating either a flat body or one nested under ``lease``."""
    if not isinstance(data, dict):
        return fallback
    if "generation" in data:
        return data.get("generation", fallback)
    lease = data.get("lease")
    if isinstance(lease, dict) and "generation" in lease:
        return lease.get("generation", fallback)
    return fallback


class GpuRouterClient:
    """Thin async HTTP wrapper over the broker REST API.

    Never raises on transport errors: returns ``(None, None)`` so callers
    fail open.  ``status`` is the HTTP status (or ``None`` on transport
    failure); ``data`` is the parsed JSON body (or ``None``).
    """

    def __init__(
        self,
        url: str,
        application_id: str,
        token: Optional[str] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.url = url.rstrip("/")
        self.application_id = application_id
        self.token = token
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        client_timeout = aiohttp.ClientTimeout(total=timeout or self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.request(
                    method,
                    f"{self.url}{path}",
                    json=json_body,
                    headers=self._headers(),
                ) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else {}
                    except ValueError:
                        data = {"raw": text}
                    return resp.status, data
        except Exception as exc:  # noqa: BLE001 — deliberately fail open
            logger.warning("GPU router %s %s failed: %s", method, path, exc)
            return None, None

    async def health(self) -> bool:
        status, _ = await self._request("GET", "/healthz", timeout=5)
        return status == 200

    async def register(self, spec: Dict[str, Any]):
        return await self._request("POST", "/v1/applications/register", spec)

    async def submit(self, request: Dict[str, Any]):
        return await self._request("POST", "/v1/requests", request)

    async def poll(self, request_id: str):
        return await self._request("GET", f"/v1/requests/{request_id}")

    async def cancel(self, request_id: str):
        return await self._request("POST", f"/v1/requests/{request_id}/cancel")

    async def renew(self, lease_id: str, generation: int):
        return await self._request(
            "POST", f"/v1/leases/{lease_id}/renew", {"generation": generation}
        )

    async def release(self, lease_id: str, generation: int):
        return await self._request(
            "POST", f"/v1/leases/{lease_id}/release", {"generation": generation}
        )

    async def get_lease(self, lease_id: str):
        return await self._request("GET", f"/v1/leases/{lease_id}")

    async def list_applications(self):
        return await self._request("GET", "/v1/applications", timeout=5)


class _LeaseSlot:
    """A warm lease held for a logical session bucket."""

    __slots__ = (
        "lease_id", "generation", "mode", "provider_id", "endpoint",
        "model", "expires_at", "idle_task", "keepalive_task", "active",
    )

    def __init__(
        self,
        lease_id: Optional[str],
        generation: int,
        mode: Optional[str],
        provider_id: Optional[str],
        endpoint: str,
        model: str,
        expires_at: float,
    ) -> None:
        self.lease_id = lease_id
        self.generation = generation
        self.mode = mode
        self.provider_id = provider_id
        self.endpoint = endpoint
        self.model = model
        self.expires_at = expires_at  # time.monotonic() seconds
        self.idle_task: Optional[asyncio.Task] = None
        self.keepalive_task: Optional[asyncio.Task] = None
        self.active = 0  # number of in-flight calls using this slot


class GpuRouterLeaseManager:
    """Acquires, renews and releases GPU leases on behalf of the LLM service.

    All public coroutines fail open — when the broker is disabled or
    unreachable ``begin`` returns the static fallback endpoint/model and a
    ``None`` token, and ``end`` becomes a no-op.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]],
        fallback_base_url: str,
        fallback_model: str,
    ) -> None:
        cfg = config or {}
        self.enabled = _env_bool("GPU_ROUTER_ENABLED", bool(cfg.get("enabled", False)))
        self.url = os.environ.get("GPU_ROUTER_URL", cfg.get("url", DEFAULT_URL))
        self.application_id = os.environ.get(
            "GPU_ROUTER_APPLICATION_ID",
            cfg.get("application_id", DEFAULT_APPLICATION_ID),
        )
        self.token = os.environ.get("GPU_ROUTER_TOKEN", cfg.get("token"))
        self.timeout = float(
            os.environ.get(
                "GPU_ROUTER_REQUEST_TIMEOUT_SECONDS",
                cfg.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            )
        )
        self.service_class = cfg.get("service_class", "TOLERANT")
        self.ttl = int(cfg.get("lease_ttl_seconds", DEFAULT_LEASE_TTL_SECONDS))
        self.idle_release = int(
            cfg.get("idle_release_seconds", DEFAULT_IDLE_RELEASE_SECONDS)
        )
        self.queue_max_wait_ms = cfg.get("queue_max_wait_ms", DEFAULT_QUEUE_MAX_WAIT_MS)
        # Circuit breaker: after a few consecutive no-grants (e.g. the broker
        # isn't managing a GPU yet) skip the broker entirely for a cooldown
        # window so queries fail open instantly instead of waiting each time.
        self.cooldown_seconds = int(cfg.get("broker_cooldown_seconds", 60))
        self._fail_threshold = int(cfg.get("broker_fail_threshold", 2))
        self._fail_count = 0
        self._cooldown_until = 0.0
        self.provider_endpoints = {
            **DEFAULT_PROVIDER_ENDPOINTS,
            **cfg.get("provider_endpoints", {}),
        }
        self.modes = cfg.get("modes", DEFAULT_MODES)
        self.fallback_base_url = fallback_base_url.rstrip("/")
        self.fallback_model = fallback_model

        self.client = GpuRouterClient(
            self.url, self.application_id, self.token, self.timeout
        )
        self._slots: Dict[str, _LeaseSlot] = {}
        self._lock = asyncio.Lock()
        self._registered = False
        self._mode_to_model = {m["name"]: m["model"] for m in self.modes}
        self._acceptable_modes = [m["name"] for m in self.modes]

    # ── Registration ────────────────────────────────────────────────
    def _build_registration(self) -> Dict[str, Any]:
        modes_spec: List[Dict[str, Any]] = []
        for m in self.modes:
            targets = [
                x["name"] for x in self.modes if x["quality_rank"] < m["quality_rank"]
            ]
            if targets:
                transition = {"type": "RESTARTABLE", "target_modes": targets}
            else:
                transition = {"type": "STOPPABLE"}
            modes_spec.append(
                {
                    "name": m["name"],
                    "quality_rank": m["quality_rank"],
                    "vram": {"peak_estimate_mb": m["vram_mb"]},
                    "transition": transition,
                }
            )
        return {
            "application_id": self.application_id,
            "service_classes_supported": [self.service_class, "BATCH"],
            "durability": "RETRYABLE",
            "modes": modes_spec,
            "placement": {
                "remote_capable": True,
                "allowed_topologies": ["LAN"],
                "checkpoint_required_on_intermittent": False,
            },
        }

    async def register(self) -> bool:
        """Register the application and its modes (idempotent, best effort).

        This broker build returns HTTP 500 when re-registering an existing
        application_id, so we first check whether we're already registered
        and skip the POST if so.
        """
        if not self.enabled or self._registered:
            return self._registered
        if await self._exists_remote():
            self._registered = True
            logger.info(
                "GPU router: application '%s' already registered", self.application_id
            )
            return True
        status, _ = await self.client.register(self._build_registration())
        if status in (200, 201):
            self._registered = True
            logger.info(
                "GPU router: registered '%s' with %d modes",
                self.application_id, len(self.modes),
            )
        elif await self._exists_remote():
            # Race or re-register 500 but the app exists — treat as registered.
            self._registered = True
            logger.info(
                "GPU router: application '%s' present after register (status=%s)",
                self.application_id, status,
            )
        else:
            logger.warning(
                "GPU router: registration failed (status=%s) — will fail open",
                status,
            )
        return self._registered

    async def _exists_remote(self) -> bool:
        _, apps = await self.client.list_applications()
        if not isinstance(apps, list):
            return False
        return any(
            isinstance(a, dict) and a.get("application_id") == self.application_id
            for a in apps
        )

    async def startup(self) -> None:
        """Best-effort health check + registration at service load."""
        if not self.enabled:
            logger.info("GPU router disabled — using static LLM endpoint %s",
                        self.fallback_base_url)
            return
        healthy = await self.client.health()
        if not healthy:
            logger.warning(
                "GPU router at %s not reachable at startup — will fail open",
                self.url,
            )
            return
        await self.register()

    # ── Lease lifecycle ─────────────────────────────────────────────
    async def begin(self, session_id: str = "app") -> Tuple[str, str, Optional[str]]:
        """Return ``(endpoint, model, token)`` to use for one LLM call.

        ``token`` is the session bucket when a real lease is in use, or
        ``None`` when we fell open to the static endpoint (so ``end`` is a
        no-op).
        """
        if not self.enabled:
            return self.fallback_base_url, self.fallback_model, None

        async with self._lock:
            if not self._registered:
                await self.register()

            slot = self._slots.get(session_id)
            now = time.monotonic()
            if slot is not None:
                self._cancel_idle(slot)
                slot.active += 1
                if slot.lease_id and (slot.expires_at - now) < RENEW_MARGIN_SECONDS:
                    await self._renew(slot)
                return slot.endpoint, slot.model, session_id

            # Circuit breaker — during a cooldown, skip the broker entirely.
            if now < self._cooldown_until:
                return self.fallback_base_url, self.fallback_model, None

            slot = await self._acquire_new(session_id)
            if slot is None:
                self._fail_count += 1
                if self._fail_count >= self._fail_threshold and self.cooldown_seconds > 0:
                    self._cooldown_until = time.monotonic() + self.cooldown_seconds
                    logger.info(
                        "GPU router: %d consecutive no-grants — cooling down %ds "
                        "(failing open to %s)",
                        self._fail_count, self.cooldown_seconds, self.fallback_base_url,
                    )
                return self.fallback_base_url, self.fallback_model, None
            self._fail_count = 0
            self._cooldown_until = 0.0
            slot.active += 1
            self._slots[session_id] = slot
            self._start_keepalive(slot)
            return slot.endpoint, slot.model, session_id

    async def end(self, token: Optional[str]) -> None:
        """Mark an LLM call finished; start the idle-release timer when the
        session goes idle."""
        if not self.enabled or token is None:
            return
        async with self._lock:
            slot = self._slots.get(token)
            if slot is None:
                return
            slot.active = max(0, slot.active - 1)
            if slot.active == 0:
                self._start_idle(slot, token)

    async def _acquire_new(self, session_id: str) -> Optional[_LeaseSlot]:
        request_id = f"{self.application_id}-{session_id}-{uuid.uuid4().hex[:12]}"
        request = {
            "request_id": request_id,
            "application_id": self.application_id,
            "workload_type": "ollama-chat",
            "service_class": self.service_class,
            "acceptable_modes": self._acceptable_modes,
            "wait_policy": {"can_queue": True, "maximum_wait_ms": self.queue_max_wait_ms},
            "placement_policy": {"remote_allowed": True, "best_effort_allowed": True},
        }
        status, data = await self.client.submit(request)
        if status is None or data is None:
            # Transport failure — best-effort cancel in case the request was
            # created server-side, then fail open.
            await self.client.cancel(request_id)
            return None

        result = data.get("result")
        if result == "QUEUED":
            data = await self._wait_for_grant(request_id) or data
            result = data.get("result")

        if result != "GRANTED":
            reason = data.get("denial_reason")
            if reason == "APPLICATION_NOT_REGISTERED":
                self._registered = False
                if await self.register():
                    status, data = await self.client.submit(request)
                    if not data or data.get("result") != "GRANTED":
                        logger.info("GPU router: not granted after re-register — failing open")
                        await self.client.cancel(request_id)
                        return None
                else:
                    await self.client.cancel(request_id)
                    return None
            else:
                logger.info(
                    "GPU router: request %s (reason=%s) — failing open to static",
                    result, reason,
                )
                # Don't leave a queued request behind that could later grant a
                # GPU we won't use.
                await self.client.cancel(request_id)
                return None

        grant = data.get("grant") or {}
        provider_id = grant.get("provider_id")
        endpoint = self.provider_endpoints.get(provider_id, self.fallback_base_url)
        mode = grant.get("mode")
        model = self._mode_to_model.get(mode, self.fallback_model)
        slot = _LeaseSlot(
            lease_id=grant.get("lease_id"),
            generation=grant.get("lease_generation", 1),
            mode=mode,
            provider_id=provider_id,
            endpoint=endpoint.rstrip("/"),
            model=model,
            expires_at=time.monotonic() + self.ttl,
        )
        logger.info(
            "GPU router: GRANTED mode=%s provider=%s model=%s endpoint=%s lease=%s",
            mode, provider_id, model, slot.endpoint, slot.lease_id,
        )
        return slot

    async def _wait_for_grant(self, request_id: str) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + (self.queue_max_wait_ms / 1000.0)
        data: Optional[Dict[str, Any]] = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            _, data = await self.client.poll(request_id)
            if data and data.get("result") in ("GRANTED", "DENIED"):
                return data
        return data

    async def _renew(self, slot: _LeaseSlot) -> None:
        if not slot.lease_id:
            return
        status, data = await self.client.renew(slot.lease_id, slot.generation)
        if status == 200:
            slot.generation = _extract_generation(data, slot.generation + 1)
            slot.expires_at = time.monotonic() + self.ttl
            logger.debug("GPU router: renewed lease %s gen=%s", slot.lease_id, slot.generation)
        elif status == 409:
            # Generation mismatch — re-fetch and adopt the current generation.
            _, fresh = await self.client.get_lease(slot.lease_id)
            slot.generation = _extract_generation(fresh, slot.generation)
            slot.expires_at = time.monotonic() + self.ttl
        else:
            logger.warning(
                "GPU router: renew failed (status=%s) for lease %s",
                status, slot.lease_id,
            )

    def _start_keepalive(self, slot: _LeaseSlot) -> None:
        if slot.keepalive_task is None and slot.lease_id:
            slot.keepalive_task = asyncio.create_task(self._keepalive_loop(slot))

    async def _keepalive_loop(self, slot: _LeaseSlot) -> None:
        interval = max(30, self.ttl // 2)
        try:
            while True:
                await asyncio.sleep(interval)
                async with self._lock:
                    if slot.lease_id is None:
                        return
                    await self._renew(slot)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("GPU router keepalive error")

    def _start_idle(self, slot: _LeaseSlot, session_id: str) -> None:
        self._cancel_idle(slot)
        slot.idle_task = asyncio.create_task(self._idle_release(slot, session_id))

    def _cancel_idle(self, slot: _LeaseSlot) -> None:
        if slot.idle_task is not None:
            slot.idle_task.cancel()
            slot.idle_task = None

    async def _idle_release(self, slot: _LeaseSlot, session_id: str) -> None:
        try:
            await asyncio.sleep(self.idle_release)
        except asyncio.CancelledError:
            return
        async with self._lock:
            current = self._slots.get(session_id)
            if current is slot and slot.active <= 0:
                await self._release_slot(session_id)

    async def _release_slot(self, session_id: str) -> None:
        slot = self._slots.pop(session_id, None)
        if slot is None:
            return
        if slot.keepalive_task:
            slot.keepalive_task.cancel()
        if slot.idle_task:
            slot.idle_task.cancel()
        if slot.lease_id:
            await self.client.release(slot.lease_id, slot.generation)
            logger.info("GPU router: released lease %s", slot.lease_id)

    async def release_all(self) -> None:
        """Release every held lease (call on shutdown)."""
        if not self.enabled:
            return
        async with self._lock:
            sessions = list(self._slots.keys())
            for sid in sessions:
                await self._release_slot(sid)
