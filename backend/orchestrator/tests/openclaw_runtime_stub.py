"""Runtime stub used by OpenClaw planner tests."""

from __future__ import annotations

from typing import Any, Dict


def run(payload: Dict[str, Any]) -> Dict[str, Any]:
    query = str(payload.get("query", ""))
    return {
        "status": "ok",
        "response": f"openclaw-runtime:{query}",
        "iterations": 2,
        "model": "openclaw-runtime-stub",
    }
