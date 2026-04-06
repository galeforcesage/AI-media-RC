#!/usr/bin/env python3
"""
main.py — Entrypoint for the SageTV MCP Server.

Usage:
    python -m src.main [--host 127.0.0.1] [--port 8766] \
        [--sagex-url http://localhost:8080] \
        [--sagex-user USER] [--sagex-pass PASS] \
        [--debug]
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import signal
import sys

from src.server import SageTVMCPServer


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SageTV MCP Server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--sagex-url", default="http://localhost:8080")
    p.add_argument("--sagex-user", default="sage")
    p.add_argument("--sagex-pass", default="")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


async def run(args: argparse.Namespace) -> None:
    config = {
        "mcp_host": args.host,
        "mcp_port": args.port,
        "sagex_url": args.sagex_url,
        "sagex_user": args.sagex_user,
        "sagex_pass": args.sagex_pass,
    }
    server = SageTVMCPServer(config)
    await server.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    await stop.wait()
    await server.stop()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
