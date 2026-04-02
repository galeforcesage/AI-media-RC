#!/usr/bin/env python3
"""
main.py — Entrypoint for the Linux MCP Server.

Usage:
    python main.py [--host 127.0.0.1] [--port 8768] [--debug]
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import signal

from src.server import LinuxMCPServer


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Linux MCP Server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8768)
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


async def run(args: argparse.Namespace) -> None:
    config = {
        "mcp_host": args.host,
        "mcp_port": args.port,
    }
    server = LinuxMCPServer(config)
    await server.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()
    await server.stop()


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
