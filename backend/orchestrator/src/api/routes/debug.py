"""
debug.py
Debug and observability endpoints: /debug/traces, /metrics.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from typing import Any, Dict, List, Optional

from utils.tracing import get_recent_traces, get_trace
from utils.metrics import render_prometheus

router = APIRouter(tags=["debug"])


@router.get("/debug/traces")
async def list_traces(limit: int = Query(default=50, le=200)) -> List[Dict[str, Any]]:
    """Return recent completed traces (newest first)."""
    return get_recent_traces(limit)


@router.get("/debug/traces/{trace_id}")
async def get_trace_detail(trace_id: str) -> Dict[str, Any]:
    """Return a specific trace by ID."""
    result = get_trace(trace_id)
    if result is None:
        return {"error": "Trace not found", "trace_id": trace_id}
    return result


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> str:
    """Prometheus-compatible metrics endpoint."""
    return render_prometheus()
