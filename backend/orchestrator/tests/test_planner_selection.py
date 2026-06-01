from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator import Orchestrator
from services.planner_registry import PlannerRegistry


class _DummyPlanner:
    pass


def _build_fake_orchestrator(default_planner: str = "agentloop"):
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {"agent": {"planner": default_planner}}
    orch._planner_registry = PlannerRegistry(orch)
    orch._planner_registry.register("agentloop", lambda: _DummyPlanner())
    orch._planner_registry.register("openclaw", lambda: _DummyPlanner())
    return orch


def test_default_planner_from_config():
    orch = _build_fake_orchestrator(default_planner="agentloop")
    planner = orch._get_planner(metadata=None)
    assert planner is not None


def test_metadata_override_planner_selection():
    orch = _build_fake_orchestrator(default_planner="agentloop")
    planner = orch._get_planner(metadata={"planner": "openclaw"})
    assert planner is not None


def test_unknown_planner_falls_back_to_agentloop():
    orch = _build_fake_orchestrator(default_planner="agentloop")
    planner = orch._get_planner(metadata={"planner": "does-not-exist"})
    assert planner is not None
