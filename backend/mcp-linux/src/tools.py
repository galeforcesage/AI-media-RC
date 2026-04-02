"""
tools.py
Complete MCP tool registry for Linux system operations.

All tools are namespaced with linux_ prefix.
Service and log operations enforce strict allowlists.
"""

from __future__ import annotations
import enum
import logging
from typing import Any, Dict

from . import system

logger = logging.getLogger(__name__)


class Safety(str, enum.Enum):
    SAFE = "SAFE"
    CONFIRM = "CONFIRM"
    DANGEROUS = "DANGEROUS"
    OWNER = "OWNER"


def _ok(data: Any = None, message: str = "OK") -> Dict[str, Any]:
    r: Dict[str, Any] = {"success": True, "message": message}
    if data is not None:
        r["data"] = data
    return r


def _fail(error: str, message: str) -> Dict[str, Any]:
    return {"success": False, "error": error, "message": message}


# ==================================================================
# Service handlers
# ==================================================================

async def _service_status(args: Dict) -> Dict:
    name = args.get("service_name", "")
    if not name:
        return _fail("missing_param", "service_name is required")
    result = await system.service_status(name)
    if "error" in result:
        return _fail("not_allowed", result["error"])
    return _ok(data=result, message=f"Service '{name}' is {result.get('state', 'unknown')}")


async def _restart_service(args: Dict) -> Dict:
    name = args.get("service_name", "")
    if not name:
        return _fail("missing_param", "service_name is required")
    result = await system.restart_service(name)
    if "error" in result:
        return _fail("restart_failed", result["error"])
    return _ok(data=result, message=f"Service '{name}' restarted")


# ==================================================================
# System info handlers
# ==================================================================

async def _disk_usage(args: Dict) -> Dict:
    result = await system.disk_usage()
    return _ok(data=result, message=f"{len(result.get('mounts', []))} mount points")


async def _network_info(args: Dict) -> Dict:
    result = await system.network_info()
    interfaces = result.get("interfaces", [])
    return _ok(data=result, message=f"{len(interfaces)} network interfaces")


async def _uptime(args: Dict) -> Dict:
    result = await system.uptime()
    return _ok(data=result, message=f"Boot time: {result.get('boot_time', '?')}")


async def _memory_info(args: Dict) -> Dict:
    result = await system.memory_info()
    total_gb = result.get("total", 0) / (1024**3)
    avail_gb = result.get("available", 0) / (1024**3)
    return _ok(data=result, message=f"{avail_gb:.1f}GB available of {total_gb:.1f}GB total")


# ==================================================================
# Log handler
# ==================================================================

async def _tail_log(args: Dict) -> Dict:
    path = args.get("path", "")
    lines = args.get("lines", 50)
    if not path:
        return _fail("missing_param", "path is required")
    result = await system.tail_log(path, lines)
    if "error" in result:
        return _fail("not_allowed", result["error"])
    return _ok(data=result, message=f"{len(result.get('lines', []))} lines from {path}")


# ==================================================================
# Docker handlers
# ==================================================================

async def _docker_ps(args: Dict) -> Dict:
    result = await system.docker_ps()
    containers = result.get("containers", [])
    return _ok(data=result, message=f"{len(containers)} containers running")


async def _docker_restart(args: Dict) -> Dict:
    container = args.get("container", "")
    if not container:
        return _fail("missing_param", "container is required")
    result = await system.docker_restart(container)
    if "error" in result:
        return _fail("not_allowed", result["error"])
    return _ok(data=result, message=f"Container '{container}' restarted")


async def _docker_logs(args: Dict) -> Dict:
    container = args.get("container", "")
    lines = args.get("lines", 50)
    if not container:
        return _fail("missing_param", "container is required")
    result = await system.docker_logs(container, lines)
    if "error" in result:
        return _fail("not_allowed", result["error"])
    return _ok(data=result, message=f"{len(result.get('lines', []))} log lines from '{container}'")


# ==================================================================
# Tool registry
# ==================================================================

TOOL_REGISTRY = {
    # --- Service tools ---
    "linux_service_status": {
        "description": "Get the status of an allowlisted system service.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service name (sagetv, channels-dvr, docker)"},
            },
            "required": ["service_name"],
        },
        "safety": Safety.SAFE,
        "handler": _service_status,
    },
    "linux_restart_service": {
        "description": "Restart an allowlisted system service.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service name (sagetv, channels-dvr, docker)"},
            },
            "required": ["service_name"],
        },
        "safety": Safety.OWNER,
        "handler": _restart_service,
    },

    # --- System info ---
    "linux_disk_usage": {
        "description": "Get disk usage for all mounted filesystems.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _disk_usage,
    },
    "linux_network_info": {
        "description": "Get network interface information.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _network_info,
    },
    "linux_uptime": {
        "description": "Get system uptime and load averages.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _uptime,
    },
    "linux_memory_info": {
        "description": "Get system memory usage.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _memory_info,
    },

    # --- Log viewing ---
    "linux_tail_log": {
        "description": "View the last N lines of an allowlisted log file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to log file (must be allowlisted)"},
                "lines": {"type": "integer", "description": "Number of lines (default 50, max 500)"},
            },
            "required": ["path"],
        },
        "safety": Safety.OWNER,
        "handler": _tail_log,
    },

    # --- Docker ---
    "linux_docker_ps": {
        "description": "List running Docker containers.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _docker_ps,
    },
    "linux_docker_restart": {
        "description": "Restart an allowlisted Docker container.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "Container name (must be allowlisted)"},
            },
            "required": ["container"],
        },
        "safety": Safety.OWNER,
        "handler": _docker_restart,
    },
    "linux_docker_logs": {
        "description": "View recent logs from an allowlisted Docker container.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "Container name (must be allowlisted)"},
                "lines": {"type": "integer", "description": "Number of lines (default 50, max 500)"},
            },
            "required": ["container"],
        },
        "safety": Safety.OWNER,
        "handler": _docker_logs,
    },
}
