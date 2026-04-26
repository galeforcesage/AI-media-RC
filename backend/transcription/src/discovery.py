"""
discovery.py
Auto-discover recording directories from SageTV and Channels DVR at startup.

SageTV: Reads recording paths from Sage.properties via sagex-api, then
  resolves Docker container-internal paths to host-visible bind-mount
  equivalents using ``docker inspect``.

Channels DVR: Queries the Channels DVR API for the DVR path
  - GET /dvr returns JSON with recording path information
"""

from __future__ import annotations
import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Recognised media extensions for quick "has recordings?" check
_MEDIA_EXTS = {".mpg", ".ts", ".mkv", ".mp4", ".avi"}


async def _mcp_call_tool(host: str, port: int, tool_name: str,
                         arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Call a tool on an MCP server and return the parsed result."""
    try:
        reader, writer = await asyncio.open_connection(host, port)
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }) + "\n"
        writer.write(request.encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=10.0)
        writer.close()
        await writer.wait_closed()
        if not line:
            return None
        resp = json.loads(line.decode())
        result = resp.get("result", {})
        content = result.get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "{}")
            return json.loads(text)
        return result
    except Exception as exc:
        logger.warning("MCP call to %s:%d tool=%s failed: %s", host, port, tool_name, exc)
        return None


async def discover_sagetv_dirs(host: str = "127.0.0.1", port: int = 8766) -> List[str]:
    """
    Discover SageTV recording directories.

    Strategy (tried in order):
    1. Read ``seeker/video_storage`` (active recording dir) and
       ``seeker/default_linux_import_paths`` (library dirs) via MCP.
    2. Read ``mf/1`` … ``mf/9`` via MCP (legacy property).
    3. Resolve any container-internal paths to host-visible bind-mounts
       by inspecting the Docker container.
    4. Fallback to well-known default paths.

    Only directories that actually exist on the **host** and contain at
    least one media file are returned.
    """
    container_paths: List[str] = []

    # --- 1. seeker/video_storage  (format: "/path,<space>,<prio>;") ---
    result = await _mcp_call_tool(host, port, "sagetv_get_config_value",
                                  {"key": "seeker/video_storage"})
    if result and result.get("success"):
        raw = result.get("data", {}).get("value", "")
        for entry in raw.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            path_part = entry.split(",")[0].strip()
            if path_part:
                container_paths.append(path_part)
                logger.info("SageTV video_storage: %s", path_part)

    # --- 2. seeker/default_linux_import_paths  (semicolon-separated) ---
    result = await _mcp_call_tool(host, port, "sagetv_get_config_value",
                                  {"key": "seeker/default_linux_import_paths"})
    if result and result.get("success"):
        raw = result.get("data", {}).get("value", "")
        for p in raw.split(";"):
            p = p.strip()
            if p and p not in container_paths:
                container_paths.append(p)
                logger.info("SageTV import path: %s", p)

    # --- 3. Legacy mf/1 … mf/9 ---
    for i in range(1, 10):
        result = await _mcp_call_tool(host, port, "sagetv_get_config_value",
                                      {"key": f"mf/{i}"})
        if result and result.get("success"):
            value = (result.get("data", {}).get("value", "") or "").strip()
            if value and value not in container_paths:
                container_paths.append(value)
                logger.info("SageTV mf/%d: %s", i, value)
        else:
            break

    # --- 4. Resolve container paths → host paths via Docker inspect ---
    mount_map = _get_docker_mount_map()

    dirs: List[str] = []
    seen: set[str] = set()

    for cp in container_paths:
        host_path = _resolve_to_host(cp, mount_map)
        if host_path and host_path not in seen:
            if Path(host_path).is_dir() and _has_media_files(host_path):
                dirs.append(host_path)
                seen.add(host_path)
                logger.info("SageTV recording dir: %s (from container %s)", host_path, cp)
            else:
                logger.debug("SageTV path skipped (empty/missing): %s -> %s", cp, host_path)

    # --- 5. Fallback ---
    if not dirs:
        for dp in ["/var/media/tv", "/opt/sagetv/server/recordings"]:
            if Path(dp).is_dir() and _has_media_files(dp):
                dirs.append(dp)
                logger.info("SageTV recording dir (fallback): %s", dp)
                break

    return dirs


def _get_docker_mount_map(container: str = "") -> Dict[str, str]:
    """Return {container_dest: host_source} from ``docker inspect``.

    Tries well-known SageTV container names if *container* is empty.
    Only bind mounts are returned (not anonymous volumes).
    """
    names = [container] if container else [
        "sagetv-mine", "sagetv-server", "sagetv",
    ]
    for name in names:
        try:
            out = subprocess.check_output(
                ["docker", "inspect", name],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
            )
            data = json.loads(out)[0]
            mapping: Dict[str, str] = {}
            for m in data.get("Mounts", []):
                if m.get("Type") == "bind":
                    mapping[m["Destination"]] = m["Source"]
            if mapping:
                logger.info("Docker mount map from %s: %d bind-mounts", name, len(mapping))
                return mapping
        except Exception:
            continue
    return {}


def _resolve_to_host(container_path: str, mount_map: Dict[str, str]) -> str:
    """Map a container-internal path to the host using bind-mount info.

    1. Exact match in mount_map → return host source.
    2. Longest-prefix match → replace prefix.
    3. Path already exists on host → return as-is (passthrough mount).
    """
    # Exact match
    if container_path in mount_map:
        return mount_map[container_path]

    # Longest prefix match
    best_dest = ""
    for dest in mount_map:
        if container_path.startswith(dest + "/") and len(dest) > len(best_dest):
            best_dest = dest
    if best_dest:
        suffix = container_path[len(best_dest):]
        return mount_map[best_dest] + suffix

    # Passthrough (same path on host and container)
    if Path(container_path).is_dir():
        return container_path

    return ""


def _has_media_files(directory: str, limit: int = 50) -> bool:
    """Quick check if a directory contains at least one media file."""
    try:
        for i, entry in enumerate(Path(directory).iterdir()):
            if i >= limit:
                return True  # many files, assume media present
            if entry.is_file() and entry.suffix.lower() in _MEDIA_EXTS:
                return True
    except OSError:
        pass
    return False


async def discover_channels_dirs(host: str = "127.0.0.1", port: int = 8767) -> List[str]:
    """
    Discover Channels DVR recording directories by querying the MCP server.
    
    Channels DVR stores recordings in a configured path, typically
    accessible via the /dvr API or as a server configuration property.
    """
    dirs: List[str] = []

    # Try getting the DVR path from the Channels MCP server
    result = await _mcp_call_tool(host, port, "channels_get_storage_status", {})
    if result and result.get("success"):
        data = result.get("data", {})
        # Primary recording path
        path_str = data.get("path", "")
        if path_str:
            # Channels stores TV recordings under <path>/TV
            tv_path = Path(path_str) / "TV"
            if tv_path.is_dir():
                dirs.append(str(tv_path))
                logger.info("Channels DVR TV dir: %s", tv_path)
            elif Path(path_str).is_dir():
                dirs.append(path_str)
                logger.info("Channels DVR recording dir: %s", path_str)
        # Extra recording paths
        for extra in data.get("extra_paths", []):
            if extra and Path(extra).is_dir():
                dirs.append(extra)
                logger.info("Channels DVR extra dir: %s", extra)
        if dirs:
            return dirs

    # Fallback: try common Channels DVR paths
    default_paths = [
        "/media/sagetv/ChannelsDVR8TB/ChannelsDVR/TV",
        "/opt/channels-dvr/data/TV",
        "/var/channels-dvr/data/TV",
    ]
    for dp in default_paths:
        if Path(dp).is_dir():
            dirs.append(dp)
            logger.info("Channels DVR recording dir (fallback): %s", dp)
            break

    return dirs


async def discover_all(
    sagetv_host: str = "127.0.0.1",
    sagetv_port: int = 8766,
    channels_host: str = "127.0.0.1",
    channels_port: int = 8767,
) -> Dict[str, List[str]]:
    """
    Discover all recording directories from both systems.
    
    Returns:
        {"sagetv": ["/path/to/recordings", ...],
         "channelsdvr": ["/path/to/recordings", ...]}
    """
    sagetv_dirs, channels_dirs = await asyncio.gather(
        discover_sagetv_dirs(sagetv_host, sagetv_port),
        discover_channels_dirs(channels_host, channels_port),
    )
    return {
        "sagetv": sagetv_dirs,
        "channelsdvr": channels_dirs,
    }
