#!/usr/bin/env python3
"""
main.py — Entrypoint for the Unified Session Manager.

Usage:
    python main.py [--host 127.0.0.1] [--port 8769] \
        [--db devices.db] [--device-limit 15] \
        [--sagetv-mcp-port 8766] [--channels-mcp-port 8767] \
        [--debug]
"""

from __future__ import annotations
import argparse
import logging

from aiohttp import web
from src.server import create_app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Unified Session Manager")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8769)
    p.add_argument("--db", default="devices.db")
    p.add_argument("--device-limit", type=int, default=15)
    p.add_argument("--sagetv-mcp-host", default="127.0.0.1")
    p.add_argument("--sagetv-mcp-port", type=int, default=8766)
    p.add_argument("--channels-mcp-host", default="127.0.0.1")
    p.add_argument("--channels-mcp-port", type=int, default=8767)
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )
    config = {
        "db_path": args.db,
        "device_limit": args.device_limit,
        "sagetv_mcp_host": args.sagetv_mcp_host,
        "sagetv_mcp_port": args.sagetv_mcp_port,
        "channels_mcp_host": args.channels_mcp_host,
        "channels_mcp_port": args.channels_mcp_port,
    }
    app = create_app(config)
    web.run_app(app, host=args.host, port=args.port, print=logger.info)


logger = logging.getLogger(__name__)

if __name__ == "__main__":
    main()
