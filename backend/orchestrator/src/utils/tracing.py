"""
tracing.py
Lightweight structured tracing using contextvars.

Provides request-scoped trace_id + hierarchical spans without external
dependencies. Spans are collected in a ring buffer and exported as
structured JSON log lines (OpenTelemetry-compatible shape).
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Context variables for propagation
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
_parent_span_var: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar("parent_span", default=None)

# Ring buffer of completed traces
_MAX_TRACES = 200
_completed_traces: collections.deque = collections.deque(maxlen=_MAX_TRACES)


def current_trace_id() -> str:
    """Get the current trace ID from context."""
    return _trace_id_var.get()


def new_trace_id() -> str:
    """Generate a new trace ID (32 hex chars)."""
    return uuid.uuid4().hex


@dataclass
class Span:
    """A single unit of work within a trace."""
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: Optional[str] = None
    name: str = ""
    service: str = "orchestrator"
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0
    duration_ms: float = 0.0
    status: str = "ok"  # ok, error
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {},
        })

    def set_error(self, error: str) -> None:
        self.status = "error"
        self.attributes["error.message"] = error

    def end(self) -> None:
        self.end_time = time.time()
        self.duration_ms = round((self.end_time - self.start_time) * 1000, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "service": self.service,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "events": self.events,
        }


@dataclass
class Trace:
    """A collection of spans sharing a trace_id."""
    trace_id: str
    spans: List[Span] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0

    def add_span(self, span: Span) -> None:
        self.spans.append(span)

    def end(self) -> None:
        self.end_time = time.time()
        _completed_traces.append(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": round((self.end_time - self.start_time) * 1000, 2) if self.end_time else 0,
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
        }


# Active traces by trace_id
_active_traces: Dict[str, Trace] = {}


def start_trace(name: str = "request", attributes: Optional[Dict[str, Any]] = None) -> Span:
    """Start a new trace and return its root span. Sets context vars."""
    trace_id = new_trace_id()
    _trace_id_var.set(trace_id)

    trace = Trace(trace_id=trace_id)
    _active_traces[trace_id] = trace

    root_span = Span(
        trace_id=trace_id,
        name=name,
        attributes=attributes or {},
    )
    trace.add_span(root_span)
    _parent_span_var.set(root_span)

    return root_span


def start_span(name: str, attributes: Optional[Dict[str, Any]] = None) -> Span:
    """Start a child span under the current context's parent span."""
    trace_id = _trace_id_var.get()
    if not trace_id:
        # No active trace — start one implicitly
        return start_trace(name, attributes)

    parent = _parent_span_var.get()
    span = Span(
        trace_id=trace_id,
        parent_span_id=parent.span_id if parent else None,
        name=name,
        attributes=attributes or {},
    )

    trace = _active_traces.get(trace_id)
    if trace:
        trace.add_span(span)

    _parent_span_var.set(span)
    return span


def end_span(span: Span) -> None:
    """End a span and restore parent context."""
    span.end()
    # Restore parent
    trace = _active_traces.get(span.trace_id)
    if trace:
        # Find the parent span
        if span.parent_span_id:
            for s in trace.spans:
                if s.span_id == span.parent_span_id:
                    _parent_span_var.set(s)
                    break
        else:
            _parent_span_var.set(None)


def end_trace(root_span: Span) -> None:
    """End the root span and finalize the trace."""
    root_span.end()
    trace_id = root_span.trace_id
    trace = _active_traces.pop(trace_id, None)
    if trace:
        trace.end()
        # Log the trace summary
        logger.info(
            "TRACE_COMPLETE trace_id=%s duration_ms=%.1f spans=%d",
            trace_id, root_span.duration_ms, len(trace.spans),
        )


def get_recent_traces(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent completed traces (newest first)."""
    traces = list(_completed_traces)
    traces.reverse()
    return [t.to_dict() for t in traces[:limit]]


def get_trace(trace_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific trace by ID (completed or active)."""
    # Check completed
    for t in _completed_traces:
        if t.trace_id == trace_id:
            return t.to_dict()
    # Check active
    if trace_id in _active_traces:
        return _active_traces[trace_id].to_dict()
    return None


class SpanContext:
    """Async context manager for spans."""

    def __init__(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        self.name = name
        self.attributes = attributes
        self.span: Optional[Span] = None

    async def __aenter__(self) -> Span:
        self.span = start_span(self.name, self.attributes)
        return self.span

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.span:
            if exc_type:
                self.span.set_error(str(exc_val))
            end_span(self.span)

    def __enter__(self) -> Span:
        self.span = start_span(self.name, self.attributes)
        return self.span

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.span:
            if exc_type:
                self.span.set_error(str(exc_val))
            end_span(self.span)


def span(name: str, attributes: Optional[Dict[str, Any]] = None) -> SpanContext:
    """Create a span context manager. Use as: async with span("name") as s: ..."""
    return SpanContext(name, attributes)
