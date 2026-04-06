"""
discovery.py
Auto-discover recording directories from SageTV and Channels DVR at startup.

SageTV: Reads recording paths from Sage.properties via sagex-api
  - Properties mf/1 through mf/9 hold the recording directory paths
  
Channels DVR: Queries the Channels DVR API for the DVR path
  - GET /dvr returns JSON with recording path information
"""

from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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
    Discover SageTV recording directories by reading Sage.properties
    via the sagex-api MCP server.
    
    SageTV stores recording paths as properties:
      mf/1, mf/2, ... mf/9  (video storage directories)
    """
    dirs: List[str] = []
    for i in range(1, 10):
        key = f"mf/{i}"
        result = await _mcp_call_tool(host, port, "sagetv_get_config_value", {"key": key})
        if result and result.get("success"):
            value = result.get("data", {}).get("value", "")
            if value and value.strip():
                path = Path(value.strip())
                if path.is_dir():
                    dirs.append(str(path))
                    logger.info("SageTV recording dir mf/%d: %s", i, path)
                else:
                    logger.warning("SageTV recording dir mf/%d path not accessible: %s", i, value)
        else:
            # No more directories configured
            break

    if not dirs:
        # Fallback: try the default SageTV recording path
        default_paths = [
            "/var/media/tv",
            "/opt/sagetv/server/recordings",
        ]
        for dp in default_paths:
            if Path(dp).is_dir():
                dirs.append(dp)
                logger.info("SageTV recording dir (fallback): %s", dp)
                break

    return dirs


async def discover_channels_dirs(host: str = "127.0.0.1", port: int = 8767) -> List[str]:
    """
    Discover Channels DVR recording directories by querying the MCP server.
    
    Channels DVR stores recordings in a configured path, typically
    accessible via the /dvr API or as a server configuration property.
    """
    dirs: List[str] = []

    # Try getting the DVR path from the Channels MCP server
    result = await _mcp_call_tool(host, port, "channels_get_dvr_path", {})
    if result and result.get("success"):
        path_str = result.get("data", {}).get("path", "")
        if path_str and Path(path_str).is_dir():
            dirs.append(path_str)
            logger.info("Channels DVR recording dir: %s", path_str)
            return dirs

    # Fallback: try getting storage info
    result = await _mcp_call_tool(host, port, "channels_get_storage", {})
    if result and result.get("success"):
        data = result.get("data", {})
        # Channels DVR returns storage paths in various formats
        for key in ("path", "recording_path", "dvr_path"):
            path_str = data.get(key, "")
            if path_str and Path(path_str).is_dir():
                dirs.append(path_str)
                logger.info("Channels DVR recording dir: %s", path_str)
                return dirs

    # Fallback: try common Channels DVR paths
    default_paths = [
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
