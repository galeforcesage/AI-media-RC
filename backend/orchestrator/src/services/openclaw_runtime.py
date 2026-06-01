"""OpenClaw runtime adapter.

This module provides a safe, configurable bridge from the orchestrator to an
OpenClaw runtime callable.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from typing import Any, Dict

from utils.logger import get_logger

logger = get_logger(__name__)


class OpenClawRuntime:
    """Loads and invokes an OpenClaw runtime callable."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._cfg = config
        self._callable = None
        self._loaded = False

    def _resolve_callable_path(self) -> str | None:
        raw = self._cfg.get("runtime_callable")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    def _load_callable(self):
        """Load callable from `module.submodule:function_name` string."""
        if self._loaded:
            return self._callable

        self._loaded = True
        path = self._resolve_callable_path()
        if not path:
            return None

        if ":" not in path:
            raise ValueError(
                "agent.openclaw.runtime_callable must be 'module.submodule:function'"
            )

        module_name, func_name = path.split(":", 1)
        module = importlib.import_module(module_name)
        fn = getattr(module, func_name, None)
        if fn is None or not callable(fn):
            raise ValueError(f"OpenClaw runtime callable not found: {path}")

        self._callable = fn
        logger.info("Loaded OpenClaw runtime callable: %s", path)
        return self._callable

    def available(self) -> bool:
        try:
            return self._load_callable() is not None
        except Exception as exc:
            logger.warning("OpenClaw runtime load failed: %s", exc)
            return False

    async def execute(self, payload: Dict[str, Any], timeout_ms: int = 30000) -> Dict[str, Any]:
        """Execute configured OpenClaw runtime callable and normalize output."""
        fn = self._load_callable()
        if fn is None:
            raise RuntimeError("OpenClaw runtime callable is not configured")

        if inspect.iscoroutinefunction(fn):
            coro = fn(payload)
        else:
            coro = asyncio.to_thread(fn, payload)

        raw = await asyncio.wait_for(coro, timeout=max(1, timeout_ms) / 1000.0)
        return self._normalize(raw)

    @staticmethod
    def _normalize(raw: Any) -> Dict[str, Any]:
        """Normalize runtime output to planner response schema."""
        if isinstance(raw, dict):
            response = (
                raw.get("response")
                or raw.get("answer")
                or raw.get("text")
                or ""
            )
            return {
                "status": raw.get("status", "ok"),
                "response": str(response),
                "iterations": int(raw.get("iterations", 1) or 1),
                "model": raw.get("model", "openclaw"),
                "raw": raw,
            }

        if isinstance(raw, str):
            return {
                "status": "ok",
                "response": raw,
                "iterations": 1,
                "model": "openclaw",
                "raw": {"text": raw},
            }

        return {
            "status": "ok",
            "response": str(raw),
            "iterations": 1,
            "model": "openclaw",
            "raw": {"value": str(raw)},
        }
