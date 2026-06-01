"""Planner registry/factory for query execution backends."""

from __future__ import annotations

from typing import Any, Callable, Dict


class PlannerRegistry:
    """Creates and caches planner instances by name."""

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator
        self._factories: Dict[str, Callable[[], Any]] = {}
        self._instances: Dict[str, Any] = {}

    def register(self, name: str, factory: Callable[[], Any]) -> None:
        self._factories[name] = factory

    def get(self, name: str) -> Any:
        if name not in self._factories:
            raise ValueError(f"Unknown planner '{name}'")
        if name not in self._instances:
            self._instances[name] = self._factories[name]()
        return self._instances[name]

    def names(self) -> list[str]:
        return sorted(self._factories.keys())
