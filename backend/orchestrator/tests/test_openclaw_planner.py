from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from services.openclaw_planner import OpenClawPlanner


class _DummyFallback:
    async def run(self, *args, **kwargs):
        return {"status": "ok", "response": "fallback", "iterations": 1}


class _DummyMCPClient:
    async def list_tools(self):
        return [
            {
                "name": "sagetv_search_recordings",
                "description": "Search SageTV recordings",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]


class _DummyOrchestrator:
    def __init__(self, enabled: bool, runtime_callable: str = ""):
        self.config = {
            "agent": {
                "openclaw": {
                    "enabled": enabled,
                    "runtime_callable": runtime_callable,
                    "timeout_ms": 30000,
                }
            }
        }
        self._sagetv = _DummyMCPClient()


@pytest.mark.asyncio
async def test_openclaw_fallback_when_disabled():
    orch = _DummyOrchestrator(enabled=False)
    planner = OpenClawPlanner(orchestrator=orch, fallback_planner=_DummyFallback())
    result = await planner.run("hi")
    assert result["response"] == "fallback"
    assert result["planner"] == "openclaw-fallback"


@pytest.mark.asyncio
async def test_openclaw_native_stub_when_enabled():
    orch = _DummyOrchestrator(enabled=True)
    planner = OpenClawPlanner(orchestrator=orch, fallback_planner=_DummyFallback())
    result = await planner.run("hi", systems=["sagetv"])
    assert result["planner"] in {"openclaw-native", "openclaw-fallback-runtime", "openclaw-native-error"}
    assert result["status"] == "ok"
    assert result.get("openai_tools_offered", 0) >= 1


@pytest.mark.asyncio
async def test_openclaw_native_runtime_callable_execution():
    orch = _DummyOrchestrator(
        enabled=True,
        runtime_callable="tests.openclaw_runtime_stub:run",
    )
    planner = OpenClawPlanner(orchestrator=orch, fallback_planner=_DummyFallback())
    result = await planner.run("hello", systems=["sagetv"])
    assert result["planner"] == "openclaw-native"
    assert result["response"] == "openclaw-runtime:hello"
    assert result["model"] == "openclaw-runtime-stub"
