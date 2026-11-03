"""Cooperative GPU broker integration for background transcription."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)


def _token_from_env_file(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip().strip("\"'") != "BROKER_API_KEYS":
                    continue
                value = value.strip().strip("\"'")
                if value.startswith("["):
                    keys = json.loads(value)
                    if isinstance(keys, list) and keys:
                        return str(keys[0]).strip() or None
                    return None
                return value.split(",", 1)[0].strip() or None
    except OSError:
        logger.exception("Could not read GPU broker environment file")
    return None


class GpuBrokerUnavailable(RuntimeError):
    """Raised when brokered GPU work cannot obtain a valid lease."""


@dataclass
class _Lease:
    request_id: str
    lease_id: str
    generation: int


class GpuBroker:
    """Own one preemptible BATCH lease for the transcription worker."""

    def __init__(
        self,
        interrupt: Callable[[], None],
        is_running: Callable[[], bool],
        unload: Callable[[], None],
        *,
        enabled: bool = True,
        url: str = "http://127.0.0.1:7235",
        application_id: str = "sagetv-transcription",
        token: Optional[str] = None,
        provider_id: str = "sagetv-cl-ollama",
        peak_vram_mb: int = 4800,
        poll_interval: float = 0.5,
    ) -> None:
        self.enabled = enabled
        self.url = url.rstrip("/")
        self.application_id = application_id
        self.token = (
            token
            or os.environ.get("GPU_BROKER_TOKEN")
            or _token_from_env_file(os.environ.get("GPU_BROKER_ENV_FILE"))
        )
        self.provider_id = provider_id
        self.peak_vram_mb = peak_vram_mb
        self.poll_interval = poll_interval
        self.worker_id = f"{application_id}-{os.getpid()}"
        self._interrupt = interrupt
        self._is_running = is_running
        self._unload = unload
        self._session: Optional[aiohttp.ClientSession] = None
        self._action_task: Optional[asyncio.Task] = None
        self._lease: Optional[_Lease] = None
        self._lock = asyncio.Lock()
        self._preempting = False

    @property
    def active(self) -> bool:
        return self._lease is not None

    @property
    def preempting(self) -> bool:
        return self._preempting

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
    ) -> tuple[int, Any]:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout, headers=self._headers())
        async with self._session.request(method, f"{self.url}{path}", json=body) as response:
            data: Any = {}
            if response.content_length != 0:
                try:
                    data = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    data = {"detail": await response.text()}
            return response.status, data

    def registration(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "service_classes_supported": ["BATCH"],
            "durability": "RETRYABLE",
            "modes": [
                {
                    "name": "whisper-large-v3",
                    "quality_rank": 1,
                    "vram": {
                        "startup_estimate_mb": 4200,
                        "steady_estimate_mb": 4000,
                        "peak_estimate_mb": self.peak_vram_mb,
                    },
                    "transition": {
                        "type": "HIBERNATABLE",
                        "safe_point": "NEXT_SEGMENT",
                        "target_modes": [],
                    },
                }
            ],
            "placement": {
                "remote_capable": False,
                "allowed_topologies": ["LOCAL", "LAN"],
                "allowed_provider_types": ["OLLAMA_ENDPOINT"],
                "checkpoint_required_on_intermittent": False,
            },
        }

    def resource_request(self, work_id: str, request_id: str) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "application_id": self.application_id,
            "workload_type": "whisper-transcription",
            "service_class": "BATCH",
            "acceptable_modes": ["whisper-large-v3"],
            "wait_policy": {
                "can_queue": True,
                "maximum_wait_ms": None,
                "wait_for_better_provider": True,
                "wake_offline_provider": False,
            },
            "placement_policy": {
                "remote_allowed": False,
                "opportunistic_allowed": False,
                "best_effort_allowed": False,
                "migration_allowed": False,
                "preferred_provider_id": self.provider_id,
            },
            "payload_reference": work_id,
        }

    async def startup(self) -> None:
        if not self.enabled:
            logger.warning("GPU broker disabled; GPU transcription is not coordinated")
            return
        if not self.token:
            raise GpuBrokerUnavailable("GPU broker token is not configured")
        status, data = await self._request(
            "POST", "/v1/applications/register", self.registration()
        )
        if status not in (200, 201):
            if self._session:
                await self._session.close()
                self._session = None
            raise GpuBrokerUnavailable(f"GPU broker registration failed: {status} {data}")
        logger.info(
            "Registered GPU broker application %s as preemptible BATCH work",
            self.application_id,
        )
        self._action_task = asyncio.create_task(self._action_loop())

    async def acquire(self, work_id: str) -> None:
        if not self.enabled:
            return
        async with self._lock:
            if self._lease is not None:
                raise RuntimeError("GPU broker lease already active")
            request_id = f"{self.application_id}:{uuid.uuid4().hex}"
            status, data = await self._request(
                "POST", "/v1/requests", self.resource_request(work_id, request_id)
            )
            if status not in (200, 201, 202):
                raise GpuBrokerUnavailable(f"GPU request failed: {status} {data}")

            while data.get("result") == "QUEUED":
                await asyncio.sleep(1)
                status, data = await self._request("GET", f"/v1/requests/{request_id}")
                if status != 200:
                    raise GpuBrokerUnavailable(
                        f"GPU request polling failed: {status} {data}"
                    )

            if data.get("result") != "GRANTED":
                reason = data.get("denial_reason") or data.get("detail") or "unknown"
                raise GpuBrokerUnavailable(f"GPU request denied: {reason}")

            grant = data.get("grant") or {}
            if grant.get("provider_id") != self.provider_id:
                await self._request("POST", f"/v1/requests/{request_id}/cancel")
                raise GpuBrokerUnavailable(
                    f"GPU broker selected unexpected provider {grant.get('provider_id')}"
                )
            self._lease = _Lease(
                request_id=request_id,
                lease_id=grant["lease_id"],
                generation=int(grant.get("lease_generation", 1)),
            )
            self._preempting = False
            await self._request("POST", f"/v1/requests/{request_id}/running")
            logger.info(
                "GPU lease granted for %s on %s (lease=%s)",
                work_id,
                self.provider_id,
                self._lease.lease_id,
            )

    async def complete(self) -> None:
        if not self.enabled:
            return
        async with self._lock:
            lease = self._lease
            self._lease = None
            if lease is None or self._preempting:
                return
            status, data = await self._request(
                "POST", f"/v1/requests/{lease.request_id}/complete"
            )
            if status not in (200, 201, 204):
                logger.warning("GPU request completion failed: %s %s", status, data)
            else:
                logger.info("Released GPU lease %s", lease.lease_id)

    async def abort(self, reason: str) -> None:
        if not self.enabled:
            return
        async with self._lock:
            lease = self._lease
            self._lease = None
            if lease is None or self._preempting:
                return
            status, data = await self._request(
                "POST", f"/v1/requests/{lease.request_id}/fail"
            )
            if status not in (200, 201, 204):
                logger.warning("GPU request failure report failed: %s %s", status, data)

    async def _action_loop(self) -> None:
        while True:
            try:
                status, actions = await self._request(
                    "GET",
                    f"/v1/lease-actions/pending?application_id={self.application_id}",
                )
                if status == 200:
                    for action in actions:
                        await self._handle_action(action)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("GPU broker action polling failed")
            await asyncio.sleep(self.poll_interval)

    async def _handle_action(self, action: dict[str, Any]) -> None:
        action_id = action["action_id"]
        generation = int(action["expected_generation"])
        status, claimed = await self._request(
            "POST",
            f"/v1/lease-actions/{action_id}/claim",
            {"expected_generation": generation, "worker_id": self.worker_id},
        )
        if status != 200:
            return

        phase = claimed.get("phase")
        if claimed.get("action") != "UNLOAD":
            await self._complete_action(claimed, "UNSUPPORTED", detail="Unsupported action")
            return

        self._preempting = True
        self._interrupt()
        deadline = time.monotonic() + 6.0
        while self._is_running() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        if self._is_running():
            await self._complete_action(
                claimed,
                "BUSY_UNTIL_SAFE_POINT",
                detail="Whisper is finishing the current segment",
            )
            return

        if phase == "PREPARE":
            await self._complete_action(
                claimed,
                "READY",
                observed_mode="quiesced",
                detail="Whisper stopped at a segment boundary",
            )
            return

        if phase == "COMMIT":
            await asyncio.to_thread(self._unload)
            await self._complete_action(
                claimed,
                "READY",
                observed_mode="unloaded",
                detail="Whisper model unloaded for higher-priority work",
            )
            self._lease = None
            logger.warning("GPU lease preempted for higher-priority work")
            return

        await self._complete_action(claimed, "READY", observed_mode="quiesced")
        self._preempting = False

    async def _complete_action(
        self,
        action: dict[str, Any],
        reply: str,
        *,
        observed_mode: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        body = {
            "expected_generation": int(action["expected_generation"]),
            "idempotency_key": action["idempotency_key"],
            "reply": reply,
            "observed_mode": observed_mode,
            "detail": detail,
        }
        status, data = await self._request(
            "POST", f"/v1/lease-actions/{action['action_id']}/complete", body
        )
        if status != 200:
            logger.warning("GPU action completion failed: %s %s", status, data)

    async def shutdown(self) -> None:
        if self._action_task:
            self._action_task.cancel()
            try:
                await self._action_task
            except asyncio.CancelledError:
                pass
            self._action_task = None
        await self.complete()
        if self._session:
            await self._session.close()
            self._session = None
