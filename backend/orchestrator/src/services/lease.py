"""
lease.py
Generic GPU-lease seam for the LLM service.

The public app ships **no** broker-specific code.  By default it uses
``NullLeaseManager`` — a no-op that always resolves to the app's static local
Ollama endpoint/model, so the assistant runs stand-alone with zero extra
dependencies.

A deployment that wants GPU arbitration installs a private plugin package (an
implementation of the ``LeaseManager`` protocol) and points the app at it with
one environment variable::

    GPU_ROUTER_PLUGIN=client.lease_manager:GpuRouterLeaseManager

At startup the orchestrator loads that ``module:attr``, instantiates it with
``(config, fallback_base_url, fallback_model)`` and hands it to ``LLMService``.
If the variable is unset, the import fails, or construction raises, the app
silently falls back to ``NullLeaseManager`` and keeps working.

The plugin is expected to *fail open* itself: any broker error must resolve to
the static fallback so the assistant never stops working when the broker is
down.
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable

from utils.logger import get_logger

logger = get_logger(__name__)

# module:attr string naming the plugin class, e.g.
# "client.lease_manager:GpuRouterLeaseManager".  Unset -> NullLeaseManager.
PLUGIN_ENV = "GPU_ROUTER_PLUGIN"


@runtime_checkable
class LeaseManager(Protocol):
    """Contract the LLM service expects from a lease manager.

    Every coroutine must fail open: on any problem, ``begin`` returns the
    static ``(base_url, model, None)`` and the rest become no-ops.
    """

    async def startup(self) -> None:
        """Best-effort health check / registration at service load."""
        ...

    async def begin(self, session_id: str = "app") -> Tuple[str, str, Optional[str]]:
        """Return ``(endpoint, model, token)`` for one LLM call.  ``token`` is
        ``None`` when running on the static fallback (so ``end`` is a no-op)."""
        ...

    async def end(self, token: Optional[str]) -> None:
        """Mark an LLM call finished; may start an idle-release timer."""
        ...

    async def release_all(self) -> None:
        """Release every held lease (call on shutdown)."""
        ...


class NullLeaseManager:
    """Default no-op manager: always uses the app's static endpoint/model."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        fallback_base_url: str = "http://127.0.0.1:11434",
        fallback_model: str = "mistral:instruct",
    ) -> None:
        self.fallback_base_url = fallback_base_url.rstrip("/")
        self.fallback_model = fallback_model
        self.enabled = False

    async def startup(self) -> None:
        logger.info(
            "No GPU lease plugin configured — using static LLM endpoint %s",
            self.fallback_base_url,
        )

    async def begin(self, session_id: str = "app") -> Tuple[str, str, Optional[str]]:
        return self.fallback_base_url, self.fallback_model, None

    async def end(self, token: Optional[str]) -> None:
        return None

    async def release_all(self) -> None:
        return None


def load_lease_manager(
    config: Optional[Dict[str, Any]],
    fallback_base_url: str,
    fallback_model: str,
) -> Any:
    """Return a ``LeaseManager``.

    Loads the plugin named by ``$GPU_ROUTER_PLUGIN`` (``module:attr``) and
    constructs it as ``Plugin(config, fallback_base_url, fallback_model)``.
    Falls back to ``NullLeaseManager`` when the variable is unset or anything
    goes wrong, so the public app never hard-depends on the private package.
    """
    spec = os.environ.get(PLUGIN_ENV, "").strip()
    if not spec:
        return NullLeaseManager(config, fallback_base_url, fallback_model)

    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        logger.warning(
            "%s=%r is not in 'module:attr' form — using static endpoint",
            PLUGIN_ENV, spec,
        )
        return NullLeaseManager(config, fallback_base_url, fallback_model)

    try:
        module = importlib.import_module(module_name)
        plugin_cls = getattr(module, attr)
        manager = plugin_cls(config or {}, fallback_base_url, fallback_model)
        logger.info("Loaded GPU lease plugin %s", spec)
        return manager
    except Exception:  # noqa: BLE001 — never let a plugin problem break startup
        logger.exception(
            "Failed to load GPU lease plugin %r — using static endpoint", spec
        )
        return NullLeaseManager(config, fallback_base_url, fallback_model)
