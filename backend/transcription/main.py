#!/usr/bin/env python3
"""
main.py — Entrypoint for the Transcription Subsystem.

Starts: file watchers, transcription worker, and MCP/resource server.

Usage:
    python main.py [--port 8770] [--debug] \
        [--sagetv-dir /path/to/sagetv/recordings] \
        [--channels-dir /path/to/channels/recordings] \
        [--ssd-temp /tmp/transcription] \
        [--whisper-model auto] \
        [--concurrency 1] \
        [--db transcription.db]
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import signal

from src.extractor import AudioExtractor
from src.queue import TranscriptionQueue
from src.server import TranscriptionServer
from src.store import MetadataStore
from src.watcher import FileWatcher
from src.whisper_engine import WhisperEngine
from src.worker import TranscriptionWorker
from src.enrichment import MetadataEnrichmentPipeline
from src.transcript_index import TranscriptIndex
from src.sidecar import TranscriptSidecar
from src.discovery import discover_all


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Transcription Subsystem")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--db", default="transcription.db")
    p.add_argument("--sagetv-dir", nargs="*", default=[], help="Override SageTV recording dir(s) (skip auto-discovery)")
    p.add_argument("--channels-dir", nargs="*", default=[], help="Override Channels DVR recording dir(s) (skip auto-discovery)")
    p.add_argument("--sagetv-mcp", default="127.0.0.1:8766", help="SageTV MCP server host:port")
    p.add_argument("--channels-mcp", default="127.0.0.1:8767", help="Channels DVR MCP server host:port")
    p.add_argument("--ssd-temp", default="/tmp/transcription")
    p.add_argument("--whisper-model", default="auto")
    p.add_argument("--whisper-device", default="auto")
    import os
    _quarter = max(1, (os.cpu_count() or 4) // 4)
    _ffmpeg = max(1, _quarter // 2)
    p.add_argument("--whisper-threads", type=int, default=_quarter, help=f"CPU threads for Whisper inference (default: {_quarter}, 25%% of cores)")
    p.add_argument("--ffmpeg-threads", type=int, default=_ffmpeg, help=f"CPU threads for ffmpeg extraction (default: {_ffmpeg})")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--gpu-idle-timeout", type=float, default=600.0,
                   help="Release Whisper/diarization models after this many seconds "
                        "with an empty queue, freeing VRAM (0 disables; default: 600)")
    p.add_argument("--no-watchers", action="store_true", help="Disable file watchers")
    p.add_argument("--no-worker", action="store_true", help="Disable transcription worker (server-only mode)")
    p.add_argument("--no-live", action="store_true", help="Disable live/incremental transcription during recording")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def _parse_addr(addr: str, default_port: int) -> tuple:
    """Parse 'host:port' string into (host, port) tuple."""
    if ":" in addr:
        host, port_str = addr.rsplit(":", 1)
        return host, int(port_str)
    return addr, default_port


async def run(args: argparse.Namespace) -> None:
    # Parse MCP endpoints
    sagetv_mcp_host, sagetv_mcp_port = _parse_addr(args.sagetv_mcp, 8766)
    channels_mcp_host, channels_mcp_port = _parse_addr(args.channels_mcp, 8767)

    # Shared components
    queue = TranscriptionQueue(db_path=args.db)
    queue.open()

    store = MetadataStore(db_path=args.db)
    store.open()

    # MCP server
    server = TranscriptionServer(
        config={"mcp_host": args.host, "mcp_port": args.port,
            "index_db": "transcript_index.db", "sidecar_dir": "sidecars"},
        queue=queue,
        store=store,
    )
    await server.start()

    tasks = []

    # File watchers — auto-discover directories if not explicitly specified
    if not args.no_watchers:
        sagetv_dirs = []
        channels_dirs = []

        if args.sagetv_dir:
            sagetv_dirs = list(args.sagetv_dir)
        if args.channels_dir:
            channels_dirs = list(args.channels_dir)

        # Auto-discover any directories not explicitly set
        if not sagetv_dirs or not channels_dirs:
            logging.getLogger(__name__).info("Auto-discovering recording directories from MCP servers...")
            discovered = await discover_all(
                sagetv_host=sagetv_mcp_host,
                sagetv_port=sagetv_mcp_port,
                channels_host=channels_mcp_host,
                channels_port=channels_mcp_port,
            )
            if not sagetv_dirs:
                sagetv_dirs = discovered.get("sagetv", [])
            if not channels_dirs:
                channels_dirs = discovered.get("channelsdvr", [])

        # Deletion callback — remove transcript data when media file disappears
        def on_file_deleted(recording_id: str):
            store.delete(recording_id)
            server.index.delete_recording(recording_id)
            logging.getLogger(__name__).info("Cleaned up transcript data for deleted file: %s", recording_id)

        # Start watchers for each discovered directory
        for i, d in enumerate(sagetv_dirs):
            name = f"sagetv-{i}" if len(sagetv_dirs) > 1 else "sagetv"
            watcher = FileWatcher(name, d, "sagetv", queue, enable_live=not args.no_live,
                                  on_file_deleted=on_file_deleted)
            tasks.append(asyncio.create_task(watcher.start()))
            logging.getLogger(__name__).info("Watching SageTV dir: %s", d)

        for i, d in enumerate(channels_dirs):
            name = f"channels-{i}" if len(channels_dirs) > 1 else "channels"
            watcher = FileWatcher(name, d, "channelsdvr", queue, enable_live=not args.no_live,
                                  on_file_deleted=on_file_deleted)
            tasks.append(asyncio.create_task(watcher.start()))
            logging.getLogger(__name__).info("Watching Channels DVR dir: %s", d)

        if not sagetv_dirs and not channels_dirs:
            logging.getLogger(__name__).warning(
                "No recording directories found. Use --sagetv-dir / --channels-dir "
                "or ensure MCP servers are reachable for auto-discovery."
            )

    # Transcription worker
    if not args.no_worker:
        extractor = AudioExtractor(ssd_temp_dir=args.ssd_temp, ffmpeg_threads=args.ffmpeg_threads)
        engine = WhisperEngine(
            model_name=args.whisper_model,
            device=args.whisper_device,
            cpu_threads=args.whisper_threads,
        )
        worker = TranscriptionWorker(
            queue=queue,
            store=store,
            extractor=extractor,
            engine=engine,
            concurrency=args.concurrency,
            gpu_idle_timeout=args.gpu_idle_timeout,
        )
        # Wire enrichment pipeline with configurable MCP endpoints
        worker.enrichment = MetadataEnrichmentPipeline(
            index=server.index,
            sidecar=server.sidecar,
            sagetv_url=args.sagetv_mcp,
            channels_url=args.channels_mcp,
        )
        tasks.append(asyncio.create_task(worker.start()))

    # Wait for shutdown signal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()

    # Cleanup
    for t in tasks:
        t.cancel()
    await server.stop()
    queue.close()
    store.close()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
