"""
gpu_arbiter.py
Single source of truth for who may use the shared GPU, and how much.

The RTX 5080 (16 GB) is shared by four tenants with a strict priority order:

    0. VSR live TV upscale  (SageTV plugin, NVENC) — TOP priority, non-cooperative.
       We cannot make it ask permission, so we *observe* it (NVENC encoder
       utilisation + free VRAM) and always leave it room.
    1. AI-remote interactive LLM (a user is waiting on an answer).
    2. AI-remote batch Whisper / diarization (delay-tolerant).
    3. Paperless easyOCR + Paperless summariser LLM (batch, lowest).

This module runs inside the orchestrator process and exposes:

  * ``select(...)``            — pick the biggest LLM that fits the room VSR left.
  * ``interactive(...)``       — async context wrapping an LLM turn: mark the
                                 session active, pause Whisper, yield the model,
                                 then resume Whisper.
  * ``can_grant(...)``         — lease decision for lower-priority tenants
                                 (Paperless), surfaced over HTTP in api/routes/gpu.py.

Everything degrades safely: if nvidia-smi or the transcription service is
unreachable we fall back to current behaviour rather than stalling anyone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

try:
    # Use the app's configured logger so arbiter decisions land in the same
    # log file/format as the rest of the orchestrator.
    from utils.logger import get_logger
    logger = get_logger(__name__)
except Exception:  # pragma: no cover - standalone/test import
    logger = logging.getLogger(__name__)


# ── Tunables (all overridable via env) ────────────────────────────────────────
# Headroom we always leave for VSR when it is live. Measured worst case:
# 1080i -> 2160p peaks at ~5.7 GB; round up for its allocator churn.
VSR_RESERVE_MB = float(os.environ.get("GPU_VSR_RESERVE_MB", "6500"))
# General safety margin so a chosen model never fills the card to the brim.
SAFETY_MB = float(os.environ.get("GPU_SAFETY_MB", "700"))
# NVENC utilisation above this % means VSR (or another encoder) is live.
ENCODER_LIVE_PCT = float(os.environ.get("GPU_ENCODER_LIVE_PCT", "3"))
# How long an interactive session stays "hot" after the last LLM token, so a
# burst of follow-up questions keeps lower-priority tenants queued.
INTERACTIVE_TTL = float(os.environ.get("GPU_INTERACTIVE_TTL", "20"))
# Keep the big model resident only briefly after a query so VSR can reclaim the
# card within seconds. 0 = unload immediately.
LLM_KEEP_ALIVE = os.environ.get("GPU_LLM_KEEP_ALIVE", "30s")

NVIDIA_SMI = os.environ.get("NVIDIA_SMI_PATH", "nvidia-smi")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
TRANSCRIPTION_HOST = os.environ.get("TRANSCRIPTION_HOST", "127.0.0.1")
TRANSCRIPTION_PORT = int(os.environ.get("TRANSCRIPTION_PORT", "8770"))


# Model ladder, biggest/most-accurate first. Footprints are measured resident
# VRAM on this card (ollama /api/ps size_vram) at the listed context. Only
# models that stay 100 % on the GPU belong here — a model that spills to CPU
# (e.g. qwen2.5:32b at 21 GB -> 7 tok/s) is deliberately excluded.
DEFAULT_LADDER: List[Dict[str, Any]] = [
    {"model": "qwen3:14b", "vram_mb": 12800, "num_ctx": 32768},
    {"model": "mistral-nemo:latest", "vram_mb": 9400, "num_ctx": 16384},
    {"model": "hermes3:8b", "vram_mb": 6800, "num_ctx": 16384},
]
# When VSR is live we must not crowd it, so we never pick above this tier.
FLOOR_MODEL = os.environ.get("GPU_FLOOR_MODEL", "hermes3:8b")


def _smi(fields: str) -> Optional[List[str]]:
    """Run nvidia-smi for the given comma-separated query fields (row 0)."""
    try:
        out = subprocess.run(
            [NVIDIA_SMI, f"--query-gpu={fields}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode != 0:
            return None
        first = out.stdout.strip().splitlines()
        if not first:
            return None
        return [c.strip() for c in first[0].split(",")]
    except Exception:
        logger.debug("nvidia-smi query failed", exc_info=True)
        return None


def gpu_free_mb() -> Optional[float]:
    """Device-wide free VRAM in MiB via nvidia-smi (never allocates a context)."""
    row = _smi("memory.free")
    if not row:
        return None
    try:
        return float(row[0])
    except ValueError:
        return None


def gpu_encoder_util() -> Optional[float]:
    """NVENC encoder utilisation %. VSR live upscale uses h264_nvenc, so a
    non-zero value is a fast, direct 'VSR is live' signal independent of VRAM."""
    row = _smi("utilization.encoder")
    if not row:
        return None
    try:
        return float(row[0])
    except ValueError:
        return None


class GpuArbiter:
    """Priority arbiter shared by the orchestrator's LLM path and (over HTTP)
    by Paperless. Thread-safety is not required: all callers are on the
    orchestrator's single asyncio loop, plus best-effort blocking probes."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        ladder = cfg.get("model_ladder") or DEFAULT_LADDER
        self.ladder: List[Dict[str, Any]] = ladder
        self.floor_model: str = cfg.get("floor_model", FLOOR_MODEL)
        self._interactive_until: float = 0.0
        self._lock = asyncio.Lock()

    # ── VSR / capacity observation ────────────────────────────────────────
    def vsr_live(self) -> bool:
        enc = gpu_encoder_util()
        return enc is not None and enc >= ENCODER_LIVE_PCT

    async def _resident_llm_mb(self) -> float:
        """VRAM currently held by Ollama models. Ollama evicts the resident
        model when we ask for another, so this memory is reclaimable and counts
        toward our budget when choosing the next model."""
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"{OLLAMA_URL}/api/ps") as r:
                    if r.status != 200:
                        return 0.0
                    data = await r.json()
            return sum(m.get("size_vram", 0) for m in data.get("models", [])) / 1e6
        except Exception:
            logger.debug("ollama /api/ps probe failed", exc_info=True)
            return 0.0

    # ── Model selection ───────────────────────────────────────────────────
    async def select(self, hint: Optional[str] = None) -> Tuple[str, Optional[int]]:
        """Choose (model, num_ctx). If VSR is live we pin the floor model so we
        never crowd live TV. Otherwise we pick the biggest tier that fits the
        room actually available (free VRAM + reclaimable resident LLM)."""
        vsr = self.vsr_live()
        if vsr:
            tier = self._tier_for(self.floor_model) or self.ladder[-1]
            logger.info("GPU select: VSR live -> floor model %s", tier["model"])
            return tier["model"], tier.get("num_ctx")

        # Concrete pin from caller/config that isn't "auto" — honour but still
        # only when it fits; otherwise fall through to automatic sizing.
        if hint and hint not in ("auto", "", None):
            tier = self._tier_for(hint)
            if tier is None:
                return hint, None

        free = gpu_free_mb()
        if free is None:
            # No nvidia-smi: don't guess big. Use the floor.
            tier = self._tier_for(self.floor_model) or self.ladder[-1]
            return tier["model"], tier.get("num_ctx")

        budget = free + await self._resident_llm_mb()
        for tier in self.ladder:  # biggest first
            if tier["vram_mb"] + SAFETY_MB <= budget:
                logger.info("GPU select: %.0f MiB budget -> %s (ctx %s)",
                            budget, tier["model"], tier.get("num_ctx"))
                return tier["model"], tier.get("num_ctx")
        tier = self.ladder[-1]
        logger.info("GPU select: tight (%.0f MiB) -> smallest %s", budget, tier["model"])
        return tier["model"], tier.get("num_ctx")

    def _tier_for(self, model: str) -> Optional[Dict[str, Any]]:
        for t in self.ladder:
            if t["model"] == model:
                return t
        return None

    # ── Interactive session state ─────────────────────────────────────────
    def mark_interactive(self, ttl: float = INTERACTIVE_TTL) -> None:
        self._interactive_until = max(self._interactive_until, time.monotonic() + ttl)

    def interactive_active(self) -> bool:
        return time.monotonic() < self._interactive_until

    def clear_interactive(self) -> None:
        # Leave a short tail so rapid follow-up questions stay prioritised.
        self._interactive_until = time.monotonic() + min(INTERACTIVE_TTL, 8.0)

    # ── Lease decisions for lower-priority tenants (Paperless) ────────────
    def can_grant(self, priority: int, vram_mb: float) -> Dict[str, Any]:
        """Grant only if nothing higher-priority is active and the request fits
        while still leaving VSR its reserve. priority: 2=batch STT, 3=paperless."""
        if self.interactive_active() and priority > 1:
            return {"granted": False, "reason": "interactive-session", "retry_after_s": 5}
        vsr = self.vsr_live()
        free = gpu_free_mb()
        if free is None:
            # Can't measure — be conservative for the lowest tier, allow others.
            return {"granted": priority <= 2, "reason": "no-nvidia-smi", "retry_after_s": 10}
        reserve = VSR_RESERVE_MB if vsr else 0.0
        if vram_mb + reserve + SAFETY_MB <= free:
            return {"granted": True, "free_mb": free, "vsr_live": vsr}
        return {"granted": False, "reason": "insufficient-vram",
                "free_mb": free, "vsr_live": vsr, "retry_after_s": 10}

    def state(self) -> Dict[str, Any]:
        return {
            "interactive_active": self.interactive_active(),
            "vsr_live": self.vsr_live(),
            "free_mb": gpu_free_mb(),
            "encoder_util": gpu_encoder_util(),
        }

    # ── Whisper pause / resume (TCP JSON-RPC to the transcription service) ──
    async def _rpc(self, method: str, timeout: float = 12.0) -> Optional[Dict[str, Any]]:
        try:
            fut = asyncio.open_connection(TRANSCRIPTION_HOST, TRANSCRIPTION_PORT)
            reader, writer = await asyncio.wait_for(fut, timeout=4)
        except Exception:
            logger.debug("transcription service unreachable for %s", method, exc_info=True)
            return None
        try:
            req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                return None
            return json.loads(line.decode())
        except Exception:
            logger.debug("RPC %s failed", method, exc_info=True)
            return None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def pause_transcription(self) -> None:
        await self._rpc("gpu/pause")

    async def resume_transcription(self) -> None:
        await self._rpc("gpu/resume")

    @asynccontextmanager
    async def interactive(self, hint: Optional[str] = None):
        """Wrap one LLM turn: mark the session active, pause Whisper to free
        VRAM, yield (model, num_ctx, keep_alive), then resume Whisper."""
        async with self._lock:  # serialise turns so we pause/resume cleanly
            self.mark_interactive()
            await self.pause_transcription()
            try:
                model, num_ctx = await self.select(hint)
                yield model, num_ctx, LLM_KEEP_ALIVE
            finally:
                self.clear_interactive()
                await self.resume_transcription()
