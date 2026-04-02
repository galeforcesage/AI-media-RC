"""
system.py
Async wrappers for Linux system commands.

All commands are executed via asyncio.create_subprocess_exec for safety.
Allowlists are enforced at this layer.
"""

from __future__ import annotations
import asyncio
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Allowlists — strictly enforced
# ------------------------------------------------------------------

ALLOWED_SERVICES = {"sagetv", "channels-dvr", "docker"}

ALLOWED_LOG_PATHS = {
    "/var/log/syslog",
    "/var/log/auth.log",
    "/var/log/kern.log",
    "/var/log/docker.log",
    "/opt/sagetv/server/logs/sagetv.log",
}

ALLOWED_DOCKER_CONTAINERS = {"sagetv-server", "samsung-tvplus-for-channels", "nextcloud-redis"}


async def _run(cmd: List[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    logger.debug("Executing: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "Command timed out"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


# ------------------------------------------------------------------
# Service operations
# ------------------------------------------------------------------

async def service_status(name: str) -> Dict:
    if name not in ALLOWED_SERVICES:
        return {"error": f"Service '{name}' not in allowlist", "allowed": list(ALLOWED_SERVICES)}
    rc, out, err = await _run(["systemctl", "is-active", name])
    state = out.strip()
    _, detail_out, _ = await _run(["systemctl", "show", name,
                                    "--property=ActiveState,SubState,MainPID,MemoryCurrent"])
    props = {}
    for line in detail_out.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return {"service": name, "state": state, "properties": props}


async def restart_service(name: str) -> Dict:
    if name not in ALLOWED_SERVICES:
        return {"error": f"Service '{name}' not in allowlist", "allowed": list(ALLOWED_SERVICES)}
    rc, out, err = await _run(["sudo", "systemctl", "restart", name], timeout=30)
    if rc != 0:
        return {"error": f"Restart failed (rc={rc}): {err.strip()}"}
    return {"service": name, "action": "restarted"}


# ------------------------------------------------------------------
# System info
# ------------------------------------------------------------------

async def disk_usage() -> Dict:
    rc, out, err = await _run(["df", "-B1", "--output=target,size,used,avail,pcent"])
    lines = out.strip().splitlines()
    mounts = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 5:
            mounts.append({
                "mount": parts[0],
                "total": int(parts[1]),
                "used": int(parts[2]),
                "available": int(parts[3]),
                "percent": parts[4],
            })
    return {"mounts": mounts}


async def network_info() -> Dict:
    rc, out, err = await _run(["ip", "-j", "addr", "show"])
    import json
    try:
        interfaces = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        # Fallback to plain text
        rc2, out2, _ = await _run(["ip", "addr", "show"])
        return {"raw": out2}
    result = []
    for iface in interfaces:
        info = {
            "name": iface.get("ifname"),
            "state": iface.get("operstate"),
            "addresses": [],
        }
        for addr_info in iface.get("addr_info", []):
            info["addresses"].append({
                "family": addr_info.get("family"),
                "address": addr_info.get("local"),
                "prefix": addr_info.get("prefixlen"),
            })
        result.append(info)
    return {"interfaces": result}


async def uptime() -> Dict:
    rc, out, _ = await _run(["uptime", "-s"])
    boot_time = out.strip()
    rc2, out2, _ = await _run(["cat", "/proc/loadavg"])
    parts = out2.strip().split()
    return {
        "boot_time": boot_time,
        "load_1m": parts[0] if len(parts) > 0 else "?",
        "load_5m": parts[1] if len(parts) > 1 else "?",
        "load_15m": parts[2] if len(parts) > 2 else "?",
    }


async def memory_info() -> Dict:
    rc, out, _ = await _run(["free", "-b"])
    lines = out.strip().splitlines()
    result = {}
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 7:
            result = {
                "total": int(parts[1]),
                "used": int(parts[2]),
                "free": int(parts[3]),
                "shared": int(parts[4]),
                "buffers": int(parts[5]),
                "available": int(parts[6]),
            }
    return result


# ------------------------------------------------------------------
# Log viewing
# ------------------------------------------------------------------

async def tail_log(path: str, lines: int = 50) -> Dict:
    # Resolve to prevent traversal
    real = os.path.realpath(path)
    if real not in ALLOWED_LOG_PATHS:
        return {"error": f"Path '{path}' not in allowlist", "allowed": list(ALLOWED_LOG_PATHS)}
    rc, out, err = await _run(["tail", "-n", str(min(lines, 500)), real])
    if rc != 0:
        return {"error": err.strip()}
    return {"path": real, "lines": out.strip().splitlines()[-min(lines, 500):]}


# ------------------------------------------------------------------
# Docker operations
# ------------------------------------------------------------------

async def docker_ps() -> Dict:
    rc, out, err = await _run(["docker", "ps", "--format",
                                "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"])
    containers = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            containers.append({
                "name": parts[0],
                "status": parts[1],
                "image": parts[2],
                "ports": parts[3] if len(parts) > 3 else "",
            })
    return {"containers": containers}


async def docker_restart(container: str) -> Dict:
    if container not in ALLOWED_DOCKER_CONTAINERS:
        return {"error": f"Container '{container}' not in allowlist",
                "allowed": list(ALLOWED_DOCKER_CONTAINERS)}
    rc, out, err = await _run(["docker", "restart", container], timeout=60)
    if rc != 0:
        return {"error": f"Restart failed: {err.strip()}"}
    return {"container": container, "action": "restarted"}


async def docker_logs(container: str, lines: int = 50) -> Dict:
    if container not in ALLOWED_DOCKER_CONTAINERS:
        return {"error": f"Container '{container}' not in allowlist",
                "allowed": list(ALLOWED_DOCKER_CONTAINERS)}
    rc, out, err = await _run(["docker", "logs", "--tail", str(min(lines, 500)), container])
    # Docker logs often go to stderr
    log_text = out or err
    return {"container": container, "lines": log_text.strip().splitlines()[-min(lines, 500):]}
