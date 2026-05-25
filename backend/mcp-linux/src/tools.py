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
# File browsing handlers (non-privileged)
# ==================================================================

async def _list_directory(args: Dict) -> Dict:
    path = args.get("path", "")
    if not path:
        return _fail("missing_param", "path is required")
    result = await system.list_directory(path)
    if "error" in result:
        return _fail("not_allowed", result["error"])
    return _ok(data=result, message=f"{result.get('count', 0)} entries in {path}")


async def _file_info(args: Dict) -> Dict:
    path = args.get("path", "")
    if not path:
        return _fail("missing_param", "path is required")
    result = await system.file_info(path)
    if "error" in result:
        return _fail("not_allowed", result["error"])
    return _ok(data=result, message=f"{result.get('type', '?')}: {path}")


# ==================================================================
# Recursive file scanning handlers
# ==================================================================

async def _find_large_files(args: Dict) -> Dict:
    root = args.get("root", "")
    if not root:
        return _fail("missing_param", "root is required")
    sort_by = args.get("sort_by", "size")
    page = int(args.get("page", 1))
    extension = args.get("extension", "")
    result = await system.find_large_files(root, sort_by=sort_by, page=page, extension=extension)
    if "error" in result:
        return _fail("not_allowed", result["error"])
    files = result.get("files", [])
    total = result.get("total_files", 0)
    pg = result.get("page", 1)
    tp = result.get("total_pages", 1)
    return _ok(data=result, message=f"{len(files)} files (page {pg}/{tp}, {total} total)")


async def _count_files(args: Dict) -> Dict:
    root = args.get("root", "")
    pattern = args.get("pattern", "")
    if not root:
        return _fail("missing_param", "root is required")
    if not pattern:
        return _fail("missing_param", "pattern is required (e.g. '*.ts')")
    result = await system.count_files_by_extension(root, pattern)
    if "error" in result:
        return _fail("not_allowed", result["error"])
    count = result.get("count", 0)
    total_size = result.get("total_size", 0)
    size_gb = total_size / (1024**3)
    return _ok(data=result, message=f"{count} files matching '{pattern}' ({size_gb:.2f} GB total)")


# ==================================================================
# Privileged: reboot / shutdown / nginx
# ==================================================================

async def _reboot_server(args: Dict) -> Dict:
    result = await system.reboot_server()
    if "error" in result:
        return _fail("reboot_failed", result["error"])
    return _ok(data=result, message="Server reboot initiated")


async def _shutdown_server(args: Dict) -> Dict:
    result = await system.shutdown_server()
    if "error" in result:
        return _fail("shutdown_failed", result["error"])
    return _ok(data=result, message="Server shutdown initiated")


async def _restart_nginx(args: Dict) -> Dict:
    result = await system.restart_nginx()
    if "error" in result:
        return _fail("restart_failed", result["error"])
    return _ok(data=result, message="Nginx restarted")


# ==================================================================
# GPU + Alerts
# ==================================================================

async def _gpu_stats(args: Dict) -> Dict:
    result = await system.gpu_stats()
    gpus = result.get("gpus", [])
    if not gpus:
        return _ok(data=result, message="No GPU detected")
    g = gpus[0]
    used = g.get("memory_used_mb") or 0
    total = g.get("memory_total_mb") or 0
    util = g.get("utilization_pct") or 0
    return _ok(data=result, message=f"{g.get('name')}: {used:.0f}/{total:.0f} MB, util {util:.0f}%")


async def _get_alerts(args: Dict) -> Dict:
    limit = int(args.get("limit", 50))
    severity = args.get("severity")
    since_ts = args.get("since_ts")
    result = await system.get_alerts(limit=limit, severity=severity, since_ts=since_ts)
    return _ok(data=result, message=f"{result.get('count', 0)} alerts")


async def _clear_alerts(args: Dict) -> Dict:
    result = await system.clear_alerts()
    if "error" in result:
        return _fail("clear_failed", result["error"])
    return _ok(data=result, message=f"Cleared {result.get('cleared', 0)} alerts")


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
        "description": "Restart an allowlisted system service. Uses passwordless sudo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service name (sagetv, channels-dvr, docker, nginx, transcription, session-manager)"},
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
        "description": "Restart an allowlisted Docker container. Uses passwordless sudo.",
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

    # --- File browsing (non-privileged) ---
    "linux_list_directory": {
        "description": "List files and directories under an allowlisted path (recording folders, project dir, transcription output).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (must be under an allowed root)"},
            },
            "required": ["path"],
        },
        "safety": Safety.SAFE,
        "handler": _list_directory,
    },
    "linux_file_info": {
        "description": "Get file/directory metadata (size, modified time, type) for an allowlisted path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory path (must be under an allowed root)"},
            },
            "required": ["path"],
        },
        "safety": Safety.SAFE,
        "handler": _file_info,
    },

    # --- Recursive file scanning ---
    "linux_find_large_files": {
        "description": "Recursively scan an allowlisted directory for files, sorted by size (largest first) or age (oldest first). Returns 15 results per page, up to 5 pages. Optionally filter by file extension.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Root directory to scan (must be under an allowed root, e.g. /var/media/tv)"},
                "sort_by": {"type": "string", "enum": ["size", "age"], "description": "Sort order: 'size' (largest first) or 'age' (oldest first). Default: size"},
                "page": {"type": "integer", "description": "Page number (1-5). Default: 1"},
                "extension": {"type": "string", "description": "Optional file extension filter (e.g. 'ts', 'mpg', 'mkv'). Omit for all files"},
            },
            "required": ["root"],
        },
        "safety": Safety.SAFE,
        "handler": _find_large_files,
    },
    "linux_count_files": {
        "description": "Recursively count files matching a glob pattern (e.g. '*.ts', '*.vprj', '*.mpg') under an allowlisted directory. Returns count, total size, plus the smallest/largest/oldest/newest matching files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Root directory to scan (must be under an allowed root)"},
                "pattern": {"type": "string", "description": "Glob pattern like '*.ts', '*.vprj', '*.mpg'"},
            },
            "required": ["root", "pattern"],
        },
        "safety": Safety.SAFE,
        "handler": _count_files,
    },

    # --- Privileged: server power ---
    "linux_reboot_server": {
        "description": "Reboot the entire Linux server. Uses passwordless sudo.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
        "safety": Safety.DANGEROUS,
        "handler": _reboot_server,
    },
    "linux_shutdown_server": {
        "description": "Shut down the entire Linux server. Uses passwordless sudo.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
        "safety": Safety.DANGEROUS,
        "handler": _shutdown_server,
    },
    "linux_restart_nginx": {
        "description": "Restart the nginx reverse proxy. Uses passwordless sudo.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
        "safety": Safety.OWNER,
        "handler": _restart_nginx,
    },

    # --- GPU + Alerts ---
    "linux_gpu_stats": {
        "description": "Live GPU statistics via nvidia-smi: utilization, memory used/free, temperature, power draw. Returns one entry per GPU. Use for 'is the GPU being used', 'GPU memory', 'GPU temperature' questions.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _gpu_stats,
    },
    "linux_get_alerts": {
        "description": "Get recent system alerts emitted by the watchdog (service crashes, crash loops, health-check failures). Each alert has ts, svc, severity (info/warning/error/critical), code, message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max alerts to return (default 50, newest first)"},
                "severity": {"type": "string", "description": "Filter by severity: info, warning, error, critical"},
                "since_ts": {"type": "string", "description": "Only return alerts at or after this ISO timestamp"},
            },
        },
        "safety": Safety.SAFE,
        "handler": _get_alerts,
    },
    "linux_clear_alerts": {
        "description": "Clear the alerts file. Returns count cleared. Safety: OWNER.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.OWNER,
        "handler": _clear_alerts,
    },
}
