"""GPU Resource Broker client and generation-fenced lease lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    """Raised when the broker rejects or returns an invalid response."""


class LeaseLostError(BrokerError):
    """Raised when a previously granted lease is no longer usable."""


@dataclass(frozen=True)
class BrokerConfig:
    base_url: str
    application_id: str
    callback_url: str
    peak_vram_mb: int
    request_timeout_seconds: float = 10.0
    retry_initial_seconds: float = 1.0
    retry_max_seconds: float = 30.0


@dataclass
class GpuLease:
    lease_id: str
    generation: int
    gpu_device_id: str
    expires_at_monotonic: float
    renewal_interval_seconds: float


class GpuBrokerClient:
    """Registers the application and owns one renewable managed-GPU lease."""

    def __init__(self, config: BrokerConfig):
        self.config = config
        self.lease: Optional[GpuLease] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._renew_task: Optional[asyncio.Task] = None
        self._lease_lost = asyncio.Event()
        self._acquire_lock = asyncio.Lock()
        self._acquisition_key: Optional[str] = None
        self._closed = False

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost.is_set()

    @property
    def has_active_lease(self) -> bool:
        return (
            self.lease is not None
            and not self._lease_lost.is_set()
            and time.monotonic() < self.lease.expires_at_monotonic
        )

    async def start(self) -> None:
        self._closed = False
        await self._register_with_retry()

    async def close(self) -> None:
        self._closed = True
        await self.release()
        if self._session:
            await self._session.close()
            self._session = None

    async def ensure_lease(self) -> GpuLease:
        """Return a live lease, retrying broker outages with capped backoff."""
        async with self._acquire_lock:
            if self.has_active_lease:
                return self.lease  # type: ignore[return-value]

            await self._cancel_renewal()
            self.lease = None
            self._lease_lost.clear()
            if self._acquisition_key is None:
                self._acquisition_key = uuid.uuid4().hex

            delay = self.config.retry_initial_seconds
            while not self._closed:
                try:
                    payload = {
                        "application_id": self.config.application_id,
                        "service_class": "BATCH",
                        "durability": "CHECKPOINTABLE",
                        "active_gpu_mode_transition": "CHECKPOINTED",
                        "placement": "LOCAL",
                        "resource": {
                            "type": "MANAGED_GPU",
                            "peak_vram_mb": self.config.peak_vram_mb,
                        },
                        "idempotency_key": self._acquisition_key,
                    }
                    data = await self._request(
                        "POST",
                        "/v1/requests",
                        payload,
                        headers={"Idempotency-Key": self._acquisition_key},
                    )
                    self.lease = self._parse_lease(data)
                    self._acquisition_key = None
                    self._lease_lost.clear()
                    self._renew_task = asyncio.create_task(self._renew_loop())
                    logger.info(
                        "GPU lease acquired: id=%s generation=%d device=%s",
                        self.lease.lease_id,
                        self.lease.generation,
                        self.lease.gpu_device_id,
                    )
                    return self.lease
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "GPU broker lease request failed (%s); retrying in %.1fs",
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.config.retry_max_seconds)
            raise BrokerError("GPU broker client is closed")

    async def release(self) -> None:
        """Release the current lease. Safe to call repeatedly."""
        await self._cancel_renewal()
        lease = self.lease
        self.lease = None
        self._lease_lost.clear()
        self._acquisition_key = None
        if lease is None:
            return
        try:
            await self._request(
                "POST",
                f"/v1/leases/{lease.lease_id}/release",
                {"generation": lease.generation},
            )
            logger.info(
                "GPU lease released: id=%s generation=%d",
                lease.lease_id,
                lease.generation,
            )
        except Exception:
            logger.exception(
                "GPU lease release failed: id=%s generation=%d",
                lease.lease_id,
                lease.generation,
            )

    def matches_lease(self, lease_id: Any, generation: Any) -> bool:
        lease = self.lease
        if lease is None:
            return False
        try:
            return lease.lease_id == str(lease_id) and lease.generation == int(generation)
        except (TypeError, ValueError):
            return False

    async def _register_with_retry(self) -> None:
        key = f"register:{self.config.application_id}"
        payload = {
            "application_id": self.config.application_id,
            "callback_url": self.config.callback_url,
            "service_class": "BATCH",
            "durability": "CHECKPOINTABLE",
            "active_gpu_mode_transition": "CHECKPOINTED",
            "peak_vram_mb": self.config.peak_vram_mb,
            "placement": "LOCAL",
            "resource_type": "MANAGED_GPU",
        }
        delay = self.config.retry_initial_seconds
        while not self._closed:
            try:
                await self._request(
                    "POST",
                    "/v1/applications/register",
                    payload,
                    headers={"Idempotency-Key": key},
                )
                logger.info(
                    "Registered GPU broker application %s as BATCH/CHECKPOINTABLE",
                    self.config.application_id,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "GPU broker registration failed (%s); retrying in %.1fs",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.retry_max_seconds)
        raise BrokerError("GPU broker client is closed")

    async def _renew_loop(self) -> None:
        try:
            while self.lease is not None and not self._closed:
                lease = self.lease
                await asyncio.sleep(max(0.1, lease.renewal_interval_seconds))
                if self.lease is not lease:
                    return
                try:
                    renewed = await self._renew_lease(lease)
                    if renewed.lease_id != lease.lease_id:
                        raise LeaseLostError("broker changed lease id during renewal")
                    if renewed.generation < lease.generation:
                        raise LeaseLostError("broker returned a stale lease generation")
                    self.lease = renewed
                except asyncio.CancelledError:
                    raise
                except LeaseLostError:
                    logger.exception("GPU lease was rejected during renewal")
                    self._lease_lost.set()
                    return
                except Exception as exc:
                    if time.monotonic() >= lease.expires_at_monotonic:
                        logger.error("GPU lease expired after renewal failures: %s", exc)
                        self._lease_lost.set()
                        return
                    logger.warning("GPU lease renewal failed before expiry: %s", exc)
        except asyncio.CancelledError:
            pass

    async def _renew_lease(self, lease: GpuLease) -> GpuLease:
        data = await self._request(
            "POST",
            f"/v1/leases/{lease.lease_id}/renew",
            {"generation": lease.generation},
        )
        return self._parse_lease(data, default_lease=lease)

    async def _cancel_renewal(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _request(
        self,
        method: str,
        path: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        url = f"{self.config.base_url.rstrip('/')}{path}"
        async with self._session.request(method, url, json=payload, headers=headers) as response:
            text = await response.text()
            if response.status in (409, 410, 412):
                raise LeaseLostError(f"broker rejected lease fence ({response.status}): {text}")
            if not 200 <= response.status < 300:
                raise BrokerError(f"broker returned {response.status}: {text}")
            if not text.strip():
                return {}
            try:
                data = await response.json()
            except Exception as exc:
                raise BrokerError(f"broker returned invalid JSON: {text[:200]}") from exc
            if not isinstance(data, dict):
                raise BrokerError("broker response must be a JSON object")
            return data

    @staticmethod
    def _parse_lease(
        data: Dict[str, Any],
        default_lease: Optional[GpuLease] = None,
    ) -> GpuLease:
        body = data.get("grant", data.get("lease", data))
        if not isinstance(body, dict):
            raise BrokerError("broker lease response is not an object")

        lease_id = body.get("lease_id") or body.get("id")
        generation = body.get("lease_generation", body.get("generation"))
        gpu_device_id = body.get("gpu_device_id", body.get("device_id"))
        if default_lease is not None:
            lease_id = lease_id or default_lease.lease_id
            generation = (
                default_lease.generation if generation is None else generation
            )
            gpu_device_id = gpu_device_id or default_lease.gpu_device_id
        if lease_id is None or generation is None or gpu_device_id is None:
            raise BrokerError(
                "broker lease response requires lease_id, lease_generation, and gpu_device_id"
            )

        ttl = GpuBrokerClient._seconds_until_expiry(body.get("expires_at"))
        if ttl is None:
            ttl = body.get("ttl_seconds", body.get("lease_ttl_seconds", 30.0))
        renew = body.get(
            "renew_after_seconds",
            body.get("renewal_interval_seconds", body.get("keep_alive")),
        )
        if isinstance(renew, bool):
            renew = None
        try:
            ttl_seconds = max(1.0, float(ttl))
            renew_seconds = max(0.1, float(renew if renew is not None else ttl_seconds / 2))
            generation_int = int(generation)
        except (TypeError, ValueError) as exc:
            raise BrokerError("broker returned invalid lease timing or generation") from exc
        return GpuLease(
            lease_id=str(lease_id),
            generation=generation_int,
            gpu_device_id=str(gpu_device_id),
            expires_at_monotonic=time.monotonic() + ttl_seconds,
            renewal_interval_seconds=min(renew_seconds, ttl_seconds * 0.8),
        )

    @staticmethod
    def _seconds_until_expiry(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                epoch = float(value)
                if epoch > 10_000_000_000:
                    epoch /= 1000
                return max(1.0, epoch - time.time())
            text = str(value).strip().replace("Z", "+00:00")
            return max(1.0, datetime.fromisoformat(text).timestamp() - time.time())
        except (TypeError, ValueError):
            raise BrokerError("broker returned invalid expires_at")
