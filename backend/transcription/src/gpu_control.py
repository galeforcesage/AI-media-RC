"""Segment-safe GPU execution and broker reclamation callbacks."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional

from .gpu_broker import GpuBrokerClient
from .whisper_engine import WhisperEngine

logger = logging.getLogger(__name__)


class ReclamationError(RuntimeError):
    """Explicit callback failure with an HTTP status."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class GpuExecutionController:
    """Prevents new GPU clips while yielding and unloads only at safe points."""

    def __init__(
        self,
        broker: GpuBrokerClient,
        engine: WhisperEngine,
        commit_timeout_seconds: float = 90.0,
    ):
        self.broker = broker
        self.engine = engine
        self.commit_timeout_seconds = commit_timeout_seconds
        self._segment_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._callback_lock = asyncio.Lock()
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._yield_armed = False
        self._active_segment = False
        self._responses: Dict[str, Dict[str, Dict[str, Any]]] = {}

    @property
    def yield_armed(self) -> bool:
        return self._yield_armed

    @property
    def active_segment(self) -> bool:
        return self._active_segment

    async def start(self) -> None:
        await self.broker.start()

    async def shutdown(self) -> None:
        self._yield_armed = True
        self._resume_event.clear()
        try:
            await asyncio.wait_for(
                self._acquire_safe_point(),
                timeout=self.commit_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for GPU segment during shutdown")
        self.engine.unload()
        await self.broker.close()

    @asynccontextmanager
    async def gpu_segment(self) -> AsyncIterator[None]:
        """Enter one bounded GPU clip with a valid lease and yield fencing."""
        while True:
            await self._resume_event.wait()
            await self.broker.ensure_lease()
            await self._segment_lock.acquire()
            if self._yield_armed or not self.broker.has_active_lease:
                self._segment_lock.release()
                continue
            self._active_segment = True
            break
        try:
            yield
        finally:
            self._active_segment = False
            self._segment_lock.release()
            if self.broker.lease_lost:
                logger.warning("GPU lease lost at clip boundary; unloading model before reacquire")
                self.engine.unload()
                await self.broker.release()

    async def release_idle(self) -> None:
        """Unload and release after a job reaches a clean idle boundary."""
        async with self._segment_lock:
            self.engine.unload()
            await self.broker.release()

    async def handle_callback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle legacy one-shot and PREPARE/COMMIT reclamation contracts."""
        async with self._callback_lock:
            return await self._handle_callback_locked(payload)

    async def _handle_callback_locked(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("action") == "checkpoint":
            key = str(payload.get("idempotency_key") or "")
            if not key:
                raise ReclamationError("idempotency_key is required", 400)
            cached = self._cached(key, "LEGACY")
            if cached:
                return cached
            await self._prepare(key, payload, validate_lease=False)
            timeout = max(0.001, float(payload.get("deadline_ms", 90000)) / 1000)
            commit = await self._commit(key, payload, timeout, validate_lease=False)
            response = {
                "safe_point_reached": True,
                "checkpoint_token": commit["checkpoint_reference"],
                "remaining_work": "queued",
                "detail": commit.get("message", "GPU lease released at clip boundary"),
            }
            self._remember(key, "LEGACY", response)
            return response

        phase = str(payload.get("phase") or "").upper()
        reclamation_id = str(payload.get("reclamation_id") or "")
        if phase not in {"PREPARE", "COMMIT"}:
            raise ReclamationError("phase must be PREPARE or COMMIT", 400)
        if not reclamation_id:
            raise ReclamationError("reclamation_id is required", 400)
        cached = self._cached(reclamation_id, phase)
        if cached:
            return cached
        if phase == "PREPARE":
            return await self._prepare(reclamation_id, payload, validate_lease=True)
        return await self._commit(
            reclamation_id,
            payload,
            self.commit_timeout_seconds,
            validate_lease=True,
        )

    async def _prepare(
        self,
        reclamation_id: str,
        payload: Dict[str, Any],
        validate_lease: bool,
    ) -> Dict[str, Any]:
        async with self._state_lock:
            cached = self._cached(reclamation_id, "PREPARE")
            if cached:
                return cached
            if validate_lease:
                self._validate_payload(payload)
            self._yield_armed = True
            self._resume_event.clear()
            response = {
                "status": "READY",
                "checkpoint_reference": f"transcription:{reclamation_id}",
                "message": "Yield armed; no new GPU clip will start",
            }
            self._remember(reclamation_id, "PREPARE", response)
            return response

    async def _commit(
        self,
        reclamation_id: str,
        payload: Dict[str, Any],
        timeout: float,
        validate_lease: bool,
    ) -> Dict[str, Any]:
        expected_reference = f"transcription:{reclamation_id}"
        if validate_lease:
            if not self._cached(reclamation_id, "PREPARE"):
                raise ReclamationError("COMMIT received before PREPARE", 409)
            self._validate_payload(payload)
            if payload.get("checkpoint_reference") != expected_reference:
                raise ReclamationError("checkpoint_reference does not match PREPARE", 400)
        self._yield_armed = True
        self._resume_event.clear()
        try:
            await asyncio.wait_for(self._acquire_safe_point(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ReclamationError(
                "safe checkpoint was not reached before the callback deadline",
                503,
            ) from exc

        self.engine.unload()
        await self.broker.release()
        utilization = await self._sample_gpu_utilization()
        response = {
            "status": "COMMITTED",
            "checkpoint_reference": expected_reference,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "gpu_utilization_pct": utilization,
            "message": "Checkpoint durable; Whisper unloaded and GPU lease released",
        }
        self._remember(reclamation_id, "COMMIT", response)
        self._yield_armed = False
        self._resume_event.set()
        return response

    async def _acquire_safe_point(self) -> None:
        async with self._segment_lock:
            return

    def _validate_payload(self, payload: Dict[str, Any]) -> None:
        required = (
            "triggering_request_id",
            "victim_request_id",
            "lease_id",
            "lease_generation",
            "gpu_device_id",
            "reason",
        )
        missing = [key for key in required if payload.get(key) is None]
        if missing:
            raise ReclamationError(f"missing callback fields: {', '.join(missing)}", 400)
        if not self.broker.matches_lease(
            payload.get("lease_id"),
            payload.get("lease_generation"),
        ):
            raise ReclamationError("callback lease fence does not match active lease", 409)
        lease = self.broker.lease
        if lease and str(payload.get("gpu_device_id")) != lease.gpu_device_id:
            raise ReclamationError("callback gpu_device_id does not match active lease", 409)

    def _cached(self, key: str, phase: str) -> Optional[Dict[str, Any]]:
        return self._responses.get(key, {}).get(phase)

    def _remember(self, key: str, phase: str, response: Dict[str, Any]) -> None:
        self._responses.setdefault(key, {})[phase] = response

    @staticmethod
    async def _sample_gpu_utilization() -> int:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                first = stdout.decode().strip().splitlines()[0]
                return max(0, min(100, int(float(first))))
        except (FileNotFoundError, IndexError, ValueError, asyncio.TimeoutError):
            pass
        return 0
