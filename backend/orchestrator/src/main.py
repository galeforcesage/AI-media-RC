#!/usr/bin/env python3
"""
main.py
Application entrypoint for the LLM Remote Orchestrator.

Responsibilities:
- Parse CLI arguments
- Load configuration
- Initialize orchestrator and all services
- Start HTTP (FastAPI/uvicorn) and MCP transports
- Handle graceful shutdown
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict

# Limit CPU threads for PyTorch/sentence-transformers BEFORE any imports
# so the embedding model doesn't starve DVR services, Ollama, or Whisper.
# Use ~12% of cores (minimum 1) for embedding — it's a background task.
_embed_threads = str(max(1, (os.cpu_count() or 4) // 8))
os.environ.setdefault("OMP_NUM_THREADS", _embed_threads)
os.environ.setdefault("MKL_NUM_THREADS", _embed_threads)
os.environ.setdefault("OPENBLAS_NUM_THREADS", _embed_threads)
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", _embed_threads)
os.environ.setdefault("NUMEXPR_NUM_THREADS", _embed_threads)

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from utils.config import load_config
from utils.logger import get_logger
from orchestrator import Orchestrator
from transport.http import router as http_router, init_http_transport
from transport.mcp import MCPServer
from services.stt import STTService

logger = get_logger(__name__)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="LLM Remote Orchestrator",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (JSON or YAML)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )
    parser.add_argument(
        "--model-path",
        type=str,
        default=None,
        help="Override LLM model path",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="HTTP server port (default: 8000)",
    )
    parser.add_argument(
        "--mcp-port",
        type=int,
        default=8765,
        help="MCP server port (default: 8765)",
    )
    return parser.parse_args()


# ------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------

def build_config(args: argparse.Namespace) -> Dict[str, Any]:
    """Build the orchestrator config dict from file + CLI overrides."""
    config: Dict[str, Any] = {}

    # Auto-discover config.json next to the project root if --config not given
    config_path = Path(args.config) if args.config else None
    if config_path is None:
        for candidate in [Path(__file__).resolve().parent.parent / "config.json",
                          Path.cwd() / "config.json"]:
            if candidate.exists():
                config_path = candidate
                break

    if config_path and config_path.exists():
        config = load_config(config_path)
        logger.info("Loaded config from %s", config_path)
    elif args.config:
        logger.warning("Config file not found: %s — using defaults", args.config)

    # CLI overrides
    if args.model_path:
        config.setdefault("llm", {})["model_path"] = args.model_path

    config["http_port"] = args.port
    config["mcp_port"] = args.mcp_port
    config["debug"] = args.debug

    return config


# ------------------------------------------------------------------
# Application factory
# ------------------------------------------------------------------

def create_app(orchestrator: Orchestrator, config: Dict[str, Any] | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="LLM Remote Orchestrator",
        description="Unified control API for SageTV + ChannelsDVR + local AI",
        version="0.2.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    init_http_transport(orchestrator)
    app.include_router(http_router, prefix="/api")

    # ── STT WebSocket ───────────────────────────────────────
    _config = config or {}
    stt = STTService(
        model_name=_config.get("stt_model", "base"),
        idle_timeout=float(_config.get("stt_idle_timeout", 300)),
    )

    @app.websocket("/ws/stt")
    async def ws_stt(ws: WebSocket):
        """
        Streaming speech-to-text via faster-whisper.

        Protocol:
          Client → Server: binary frames (PCM16-LE mono 16kHz)
          Client → Server: JSON {"action": "stop"} to end
          Server → Client: JSON {"type": "partial", "text": "..."}
          Server → Client: JSON {"type": "final", "text": "..."}
        """
        await ws.accept()
        audio_buf = bytearray()
        partial_task = None
        stop_event = asyncio.Event()

        async def emit_partials():
            """Periodically transcribe accumulated audio for live feedback."""
            while not stop_event.is_set():
                await asyncio.sleep(1.5)
                if stop_event.is_set():
                    break
                buf_copy = bytes(audio_buf)
                if len(buf_copy) < 6400:  # <0.2s
                    continue
                try:
                    text = await stt.transcribe_pcm(buf_copy)
                    if text:
                        await ws.send_json({"type": "partial", "text": text})
                except Exception:
                    pass

        try:
            partial_task = asyncio.create_task(emit_partials())
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.receive":
                    if "bytes" in msg and msg["bytes"]:
                        audio_buf.extend(msg["bytes"])
                    elif "text" in msg and msg["text"]:
                        import json as _json
                        try:
                            data = _json.loads(msg["text"])
                        except ValueError:
                            continue
                        if data.get("action") == "stop":
                            break
                elif msg["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            stop_event.set()
            if partial_task:
                partial_task.cancel()
                try:
                    await partial_task
                except asyncio.CancelledError:
                    pass

        # Final transcription of full audio
        if audio_buf:
            try:
                text = await stt.transcribe_pcm(bytes(audio_buf))
                await ws.send_json({"type": "final", "text": text or ""})
            except Exception as exc:
                logger.warning("STT final transcription failed: %s", exc)
                try:
                    await ws.send_json({"type": "final", "text": ""})
                except Exception:
                    pass

        try:
            await ws.close()
        except Exception:
            pass

    @app.on_event("startup")
    async def on_startup() -> None:
        await orchestrator.initialize()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await orchestrator.shutdown()
        await stt.aclose()

    return app


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

async def run(args: argparse.Namespace) -> None:
    """Async entrypoint: starts orchestrator, HTTP, and MCP servers."""
    config = build_config(args)

    if config.get("debug"):
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")

    orchestrator = Orchestrator(config)
    app = create_app(orchestrator, config)

    # MCP server
    mcp = MCPServer(host="127.0.0.1", port=config["mcp_port"])
    mcp.bind_orchestrator(orchestrator)

    # HTTP server via uvicorn
    uv_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=config["http_port"],
        log_level="debug" if config.get("debug") else "info",
    )
    http_server = uvicorn.Server(uv_config)

    # Graceful shutdown event
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows does not support add_signal_handler for all signals
            pass

    # Start MCP
    await mcp.start()
    logger.info("MCP server running on 127.0.0.1:%d", config["mcp_port"])

    # Start HTTP (runs until stop_event or server shutdown)
    http_task = asyncio.create_task(http_server.serve())
    logger.info("HTTP server running on 0.0.0.0:%d", config["http_port"])

    # Wait for shutdown signal
    await stop_event.wait()

    # Cleanup
    logger.info("Initiating graceful shutdown …")
    http_server.should_exit = True
    await http_task
    await mcp.stop()
    logger.info("All servers stopped")


def main() -> None:
    """CLI entrypoint."""
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
