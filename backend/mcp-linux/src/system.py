"""
system.py
Async wrappers for Linux system commands.

All commands are executed via asyncio.create_subprocess_exec for safety.
Allowlists are enforced at this layer.
"""

from __future__ import annotations
import asyncio
import fnmatch
import logging
import os
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Allowlists — strictly enforced
# ------------------------------------------------------------------

ALLOWED_SERVICES = {
    "sagetv", "channels-dvr", "docker", "nginx",
    "transcription", "session-manager",
}

ALLOWED_LOG_PATHS = {
    "/var/log/syslog",
    "/var/log/auth.log",
    "/var/log/kern.log",
    "/var/log/docker.log",
    "/var/log/nginx/error.log",
    "/var/log/nginx/access.log",
    "/opt/sagetv/server/logs/sagetv.log",
    "/tmp/orchestrator.log",
    "/tmp/transcription.log",
    "/tmp/session-manager.log",
    "/tmp/mcp-sagetv.log",
    "/tmp/mcp-channels.log",
    "/tmp/mcp-linux.log",
}

ALLOWED_DOCKER_CONTAINERS = {"sagetv-server", "samsung-tvplus-for-channels", "nextcloud-redis"}

ALLOWED_BROWSE_ROOTS = {
    "/var/media/tv",
    "/var/media/channels",
    "/media/sagetv",
    os.path.expanduser("~/AI-media-RC"),
    "/tmp/transcription",
}


async def _run(cmd: List[str], timeout: int = 15, stdin_data: Optional[str] = None) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    logger.debug("Executing: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode() if stdin_data else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "Command timed out"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _run_sudo(cmd: List[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command with sudo (passwordless via sudoers config)."""
    full_cmd = ["sudo", "-n"] + cmd
    return await _run(full_cmd, timeout=timeout)


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
    rc, out, err = await _run_sudo(["systemctl", "restart", name], timeout=30)
    if rc != 0:
        # Strip sudo prompt noise from stderr
        err_clean = "\n".join(l for l in err.strip().splitlines() if not l.startswith("[sudo]"))
        return {"error": f"Restart failed (rc={rc}): {err_clean.strip()}"}
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
    rc, out, err = await _run_sudo(["docker", "restart", container], timeout=60)
    if rc != 0:
        err_clean = "\n".join(l for l in err.strip().splitlines() if not l.startswith("[sudo]"))
        return {"error": f"Restart failed: {err_clean.strip()}"}
    return {"container": container, "action": "restarted"}


async def docker_logs(container: str, lines: int = 50) -> Dict:
    if container not in ALLOWED_DOCKER_CONTAINERS:
        return {"error": f"Container '{container}' not in allowlist",
                "allowed": list(ALLOWED_DOCKER_CONTAINERS)}
    rc, out, err = await _run(["docker", "logs", "--tail", str(min(lines, 500)), container])
    # Docker logs often go to stderr
    log_text = out or err
    return {"container": container, "lines": log_text.strip().splitlines()[-min(lines, 500):]}


# ------------------------------------------------------------------
# File browsing (non-privileged)
# ------------------------------------------------------------------

def _is_under_allowed_root(path: str) -> bool:
    """Check if a resolved path is under an allowed browse root."""
    real = os.path.realpath(path)
    return any(real == root or real.startswith(root + "/") for root in ALLOWED_BROWSE_ROOTS)


async def list_directory(path: str) -> Dict:
    """List files/dirs in an allowlisted directory."""
    real = os.path.realpath(path)
    if not _is_under_allowed_root(real):
        return {"error": f"Path not under allowed roots", "allowed": list(ALLOWED_BROWSE_ROOTS)}
    if not os.path.isdir(real):
        return {"error": f"Not a directory: {real}"}
    entries = []
    try:
        for name in sorted(os.listdir(real)):
            full = os.path.join(real, name)
            try:
                st = os.stat(full)
                entries.append({
                    "name": name,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": st.st_size,
                    "modified": st.st_mtime,
                })
            except OSError:
                entries.append({"name": name, "type": "unknown", "size": 0, "modified": 0})
    except PermissionError:
        return {"error": f"Permission denied: {real}"}
    return {"path": real, "entries": entries, "count": len(entries)}


async def file_info(path: str) -> Dict:
    """Get file metadata for an allowlisted path."""
    real = os.path.realpath(path)
    if not _is_under_allowed_root(real):
        return {"error": f"Path not under allowed roots", "allowed": list(ALLOWED_BROWSE_ROOTS)}
    if not os.path.exists(real):
        return {"error": f"Path not found: {real}"}
    st = os.stat(real)
    return {
        "path": real,
        "type": "dir" if os.path.isdir(real) else "file",
        "size": st.st_size,
        "modified": st.st_mtime,
        "readable": os.access(real, os.R_OK),
    }


# ------------------------------------------------------------------
# Recursive file scanning
# ------------------------------------------------------------------

_MAX_SCAN_FILES = 100_000  # safety cap on files walked
_PAGE_SIZE = 15
_MAX_PAGES = 5


async def find_large_files(
    root: str,
    sort_by: str = "size",
    page: int = 1,
    extension: str = "",
) -> Dict:
    """
    Recursively scan an allowed root for files, sorted by size (desc)
    or age (oldest first).  Returns 15 results per page, up to 5 pages.
    """
    real = os.path.realpath(root)
    if not _is_under_allowed_root(real):
        return {"error": "Path not under allowed roots", "allowed": list(ALLOWED_BROWSE_ROOTS)}
    if not os.path.isdir(real):
        return {"error": f"Not a directory: {real}"}
    if page < 1 or page > _MAX_PAGES:
        return {"error": f"Page must be 1-{_MAX_PAGES}"}
    if sort_by not in ("size", "age"):
        return {"error": "sort_by must be 'size' or 'age'"}

    ext_filter = extension.lower().lstrip(".") if extension else ""
    files: List[Dict] = []
    scanned = 0

    for dirpath, _dirnames, filenames in os.walk(real):
        for fname in filenames:
            scanned += 1
            if scanned > _MAX_SCAN_FILES:
                break
            if ext_filter and not fname.lower().endswith(f".{ext_filter}"):
                continue
            full = os.path.join(dirpath, fname)
            try:
                st = os.stat(full)
                files.append({
                    "path": full,
                    "name": fname,
                    "size": st.st_size,
                    "modified": st.st_mtime,
                    "age_days": round((time.time() - st.st_mtime) / 86400, 1),
                })
            except OSError:
                continue
        if scanned > _MAX_SCAN_FILES:
            break

    # Sort
    if sort_by == "size":
        files.sort(key=lambda f: f["size"], reverse=True)
    else:  # age — oldest first
        files.sort(key=lambda f: f["modified"])

    total = len(files)
    total_pages = min((total + _PAGE_SIZE - 1) // _PAGE_SIZE, _MAX_PAGES)
    start = (page - 1) * _PAGE_SIZE
    end = start + _PAGE_SIZE
    page_files = files[start:end]

    return {
        "root": real,
        "sort_by": sort_by,
        "extension_filter": ext_filter or None,
        "total_files": total,
        "total_scanned": scanned,
        "page": page,
        "total_pages": total_pages,
        "page_size": _PAGE_SIZE,
        "files": page_files,
    }


async def count_files_by_extension(root: str, pattern: str) -> Dict:
    """
    Recursively count files matching a glob pattern (e.g. '*.ts', '*.vprj')
    under an allowed root.  Also returns total size of matched files.
    """
    real = os.path.realpath(root)
    if not _is_under_allowed_root(real):
        return {"error": "Path not under allowed roots", "allowed": list(ALLOWED_BROWSE_ROOTS)}
    if not os.path.isdir(real):
        return {"error": f"Not a directory: {real}"}
    if not pattern or not pattern.startswith("*."):
        return {"error": "Pattern must be a glob like '*.ts' or '*.vprj'"}

    pat = pattern.lower()
    count = 0
    total_size = 0
    smallest = None
    largest = None
    oldest = None
    newest = None
    scanned = 0

    for dirpath, _dirnames, filenames in os.walk(real):
        for fname in filenames:
            scanned += 1
            if scanned > _MAX_SCAN_FILES:
                break
            if not fnmatch.fnmatch(fname.lower(), pat):
                continue
            full = os.path.join(dirpath, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            count += 1
            total_size += st.st_size
            entry = {"path": full, "name": fname, "size": st.st_size, "modified": st.st_mtime}
            if smallest is None or st.st_size < smallest["size"]:
                smallest = entry
            if largest is None or st.st_size > largest["size"]:
                largest = entry
            if oldest is None or st.st_mtime < oldest["modified"]:
                oldest = entry
            if newest is None or st.st_mtime > newest["modified"]:
                newest = entry
        if scanned > _MAX_SCAN_FILES:
            break

    return {
        "root": real,
        "pattern": pattern,
        "count": count,
        "total_size": total_size,
        "total_scanned": scanned,
        "smallest": smallest,
        "largest": largest,
        "oldest": oldest,
        "newest": newest,
    }


# ------------------------------------------------------------------
# Privileged: reboot / shutdown
# ------------------------------------------------------------------

async def reboot_server() -> Dict:
    """Reboot the entire Linux server."""
    rc, out, err = await _run_sudo(["reboot"], timeout=15)
    if rc != 0:
        err_clean = "\n".join(l for l in err.strip().splitlines() if not l.startswith("[sudo]"))
        return {"error": f"Reboot failed: {err_clean.strip()}"}
    return {"action": "reboot", "status": "initiated"}


async def shutdown_server() -> Dict:
    """Shut down the entire Linux server."""
    rc, out, err = await _run_sudo(["shutdown", "-h", "now"], timeout=15)
    if rc != 0:
        err_clean = "\n".join(l for l in err.strip().splitlines() if not l.startswith("[sudo]"))
        return {"error": f"Shutdown failed: {err_clean.strip()}"}
    return {"action": "shutdown", "status": "initiated"}


async def restart_nginx() -> Dict:
    """Restart nginx specifically (common operation)."""
    rc, out, err = await _run_sudo(["systemctl", "restart", "nginx"], timeout=30)
    if rc != 0:
        err_clean = "\n".join(l for l in err.strip().splitlines() if not l.startswith("[sudo]"))
        return {"error": f"Nginx restart failed: {err_clean.strip()}"}
    return {"service": "nginx", "action": "restarted"}
