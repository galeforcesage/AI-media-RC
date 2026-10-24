"""
metrics.py
Lightweight in-process metrics: counters, gauges, and latency histograms.

Exposes a Prometheus-compatible text format at /metrics.
No external dependencies.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional


class Counter:
    """Monotonically increasing counter."""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._value = 0.0
        self._lock = threading.Lock()
        self._labels: Dict[str, float] = {}

    def inc(self, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            if labels:
                key = _labels_key(labels)
                self._labels[key] = self._labels.get(key, 0.0) + value
            else:
                self._value += value

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        with self._lock:
            if labels:
                return self._labels.get(_labels_key(labels), 0.0)
            return self._value

    def to_prometheus(self) -> str:
        lines = []
        if self.description:
            lines.append(f"# HELP {self.name} {self.description}")
        lines.append(f"# TYPE {self.name} counter")
        with self._lock:
            if self._labels:
                for key, val in sorted(self._labels.items()):
                    lines.append(f"{self.name}{{{key}}} {val}")
            else:
                lines.append(f"{self.name} {self._value}")
        return "\n".join(lines)


class Gauge:
    """A value that can go up and down."""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._value = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    def inc(self, value: float = 1.0) -> None:
        with self._lock:
            self._value += value

    def dec(self, value: float = 1.0) -> None:
        with self._lock:
            self._value -= value

    def get(self) -> float:
        with self._lock:
            return self._value

    def to_prometheus(self) -> str:
        lines = []
        if self.description:
            lines.append(f"# HELP {self.name} {self.description}")
        lines.append(f"# TYPE {self.name} gauge")
        lines.append(f"{self.name} {self._value}")
        return "\n".join(lines)


class Histogram:
    """Latency histogram with fixed buckets."""

    DEFAULT_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)

    def __init__(self, name: str, description: str = "", buckets: Optional[tuple] = None):
        self.name = name
        self.description = description
        self.buckets = buckets or self.DEFAULT_BUCKETS
        self._counts = [0] * len(self.buckets)
        self._sum = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            for i, b in enumerate(self.buckets):
                if value <= b:
                    self._counts[i] += 1

    def to_prometheus(self) -> str:
        lines = []
        if self.description:
            lines.append(f"# HELP {self.name} {self.description}")
        lines.append(f"# TYPE {self.name} histogram")
        with self._lock:
            cumulative = 0
            for i, b in enumerate(self.buckets):
                cumulative += self._counts[i]
                lines.append(f'{self.name}_bucket{{le="{b}"}} {cumulative}')
            lines.append(f'{self.name}_bucket{{le="+Inf"}} {self._count}')
            lines.append(f"{self.name}_sum {self._sum:.4f}")
            lines.append(f"{self.name}_count {self._count}")
        return "\n".join(lines)


def _labels_key(labels: Dict[str, str]) -> str:
    """Convert labels dict to Prometheus label string."""
    parts = [f'{k}="{v}"' for k, v in sorted(labels.items())]
    return ",".join(parts)


# ------------------------------------------------------------------
# Global metrics registry
# ------------------------------------------------------------------

_metrics: Dict[str, object] = {}


def counter(name: str, description: str = "") -> Counter:
    if name not in _metrics:
        _metrics[name] = Counter(name, description)
    return _metrics[name]


def gauge(name: str, description: str = "") -> Gauge:
    if name not in _metrics:
        _metrics[name] = Gauge(name, description)
    return _metrics[name]


def histogram(name: str, description: str = "", buckets: Optional[tuple] = None) -> Histogram:
    if name not in _metrics:
        _metrics[name] = Histogram(name, description, buckets)
    return _metrics[name]


def render_prometheus() -> str:
    """Render all registered metrics in Prometheus text exposition format."""
    sections = []
    for m in _metrics.values():
        sections.append(m.to_prometheus())
    return "\n\n".join(sections) + "\n"


# ------------------------------------------------------------------
# Pre-defined application metrics
# ------------------------------------------------------------------

# HTTP
http_requests_total = counter("http_requests_total", "Total HTTP requests")
http_request_duration = histogram("http_request_duration_seconds", "HTTP request latency")
http_errors_total = counter("http_errors_total", "Total HTTP error responses")

# LLM
llm_requests_total = counter("llm_requests_total", "Total LLM inference requests")
llm_request_duration = histogram("llm_request_duration_seconds", "LLM inference latency")
llm_errors_total = counter("llm_errors_total", "Total LLM errors")
llm_tokens_generated = counter("llm_tokens_generated_total", "Total LLM tokens generated")

# Agent loop
agent_iterations_total = counter("agent_iterations_total", "Total agent loop iterations")
agent_tool_calls_total = counter("agent_tool_calls_total", "Total tool calls made by agent")
agent_tool_duration = histogram("agent_tool_duration_seconds", "Tool call latency")
agent_tool_errors_total = counter("agent_tool_errors_total", "Total tool call errors")

# MCP
mcp_calls_total = counter("mcp_calls_total", "Total outbound MCP calls")
mcp_call_duration = histogram("mcp_call_duration_seconds", "Outbound MCP call latency")
mcp_errors_total = counter("mcp_errors_total", "Total MCP call errors")

# Transcription
transcription_jobs_total = counter("transcription_jobs_total", "Total transcription jobs processed")
transcription_job_duration = histogram("transcription_job_duration_seconds", "Transcription job processing time")
transcription_errors_total = counter("transcription_errors_total", "Total transcription errors")
transcription_queue_depth = gauge("transcription_queue_depth", "Current transcription queue depth")
