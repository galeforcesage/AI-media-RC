#!/usr/bin/env python3
"""
ollama-priority-proxy.py

A tiny reverse proxy that sits in front of Ollama for *low-priority* tenants
(Paperless-AI's summariser LLM, and anything else that must yield to the
AI-media-remote and VSR).

Paperless-AI talks to the same shared Ollama as the AI-remote. Point it at this
proxy instead of Ollama directly, and every generate/chat request first asks the
GPU arbiter for a lease. While an AI-remote interactive session or VSR live TV
is active the arbiter denies the lease and the proxy holds the request (polling)
until it's granted, so Paperless never yanks the card mid-answer or mid-upscale.

All other Ollama endpoints (tags, ps, embeddings, pulls) pass straight through.

Run on the server (host network), e.g.:
    OLLAMA_URL=http://127.0.0.1:11434 \
    ARBITER_URL=http://127.0.0.1:8000/api \
    LISTEN_PORT=11500 \
    python3 ollama-priority-proxy.py

Then set Paperless-AI's Ollama base URL to http://<host>:11500 .

Only depends on the Python 3 stdlib (http.server, urllib) — no pip installs.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
ARBITER_URL = os.environ.get("ARBITER_URL", "http://127.0.0.1:8000/api").rstrip("/")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "11500"))
GUARD_VRAM = float(os.environ.get("GPU_GUARD_VRAM", "10000"))  # a 14b LLM is ~10 GB
GUARD_PRIO = int(os.environ.get("GPU_GUARD_PRIO", "3"))
POLL = float(os.environ.get("GPU_GUARD_POLL", "5"))
MAX_WAIT = float(os.environ.get("GPU_GUARD_MAX_WAIT", "1800"))

# Endpoints that actually consume the GPU and must be gated.
GATED = ("/api/generate", "/api/chat")


def _acquire_lease() -> None:
    """Block until the arbiter grants a lease (or we hit MAX_WAIT / it's down)."""
    body = json.dumps({
        "tenant": "paperless-ai", "priority": GUARD_PRIO, "vram_mb": GUARD_VRAM,
    }).encode()
    start = time.monotonic()
    while True:
        try:
            req = urllib.request.Request(
                f"{ARBITER_URL}/gpu/lease", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                decision = json.loads(r.read().decode())
        except Exception:
            return  # arbiter down -> fail open
        if decision.get("granted"):
            return
        if time.monotonic() - start >= MAX_WAIT:
            return  # don't starve Paperless forever
        time.sleep(POLL)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        payload = self.rfile.read(length) if length else None

        if self.path in GATED:
            _acquire_lease()

        url = f"{OLLAMA_URL}{self.path}"
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length")}
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # noqa: BLE001
            msg = json.dumps({"error": f"proxy: {e}"}).encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")


def main() -> None:
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"ollama-priority-proxy: {LISTEN_HOST}:{LISTEN_PORT} -> {OLLAMA_URL} "
          f"(arbiter {ARBITER_URL}, gating {GATED})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
