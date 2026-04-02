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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Transcription Subsystem")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--db", default="transcription.db")
    p.add_argument("--sagetv-dir", default="")
    p.add_argument("--channels-dir", default="")
    p.add_argument("--ssd-temp", default="/tmp/transcription")
    p.add_argument("--whisper-model", default="auto")
    p.add_argument("--whisper-device", default="auto")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--no-watchers", action="store_true", help="Disable file watchers")
    p.add_argument("--no-worker", action="store_true", help="Disable transcription worker (server-only mode)")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


async def run(args: argparse.Namespace) -> None:
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

    # File watchers
    if not args.no_watchers:
        if args.sagetv_dir:
            watcher_sage = FileWatcher("sagetv", args.sagetv_dir, "sagetv", queue)
            tasks.append(asyncio.create_task(watcher_sage.start()))
        if args.channels_dir:
            watcher_ch = FileWatcher("channels", args.channels_dir, "channelsdvr", queue)
            tasks.append(asyncio.create_task(watcher_ch.start()))

    # Transcription worker
    if not args.no_worker:
        extractor = AudioExtractor(ssd_temp_dir=args.ssd_temp)
        engine = WhisperEngine(
            model_name=args.whisper_model,
            device=args.whisper_device,
        )
        worker = TranscriptionWorker(
            queue=queue,
            store=store,
            extractor=extractor,
            engine=engine,
            concurrency=args.concurrency,
        )
        # Wire enrichment pipeline so worker populates the transcript index
        worker.enrichment = MetadataEnrichmentPipeline(
            index=server.index,
            sidecar=server.sidecar,
            sagetv_url="127.0.0.1:8766",
            channels_url="127.0.0.1:8767",
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
