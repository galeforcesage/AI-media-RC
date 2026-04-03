"""
system.py
System diagnostics and control service.
Provides CPU, RAM, disk, GPU availability, and system-level commands
such as volume, reboot, and shutdown.  Privileged commands accept an
optional sudo_password which is fed to ``sudo -S`` via stdin so
the frontend can prompt interactively.
"""

from __future__ import annotations
import asyncio
import logging
import os
import platform
import shutil
from typing import Any, Dict

from models.system import SystemDiagnostics, SystemInfo

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Allowlists  (mirrors mcp-linux for defense-in-depth)
# ------------------------------------------------------------------
ALLOWED_DOCKER_CONTAINERS = {"sagetv-server", "samsung-tvplus-for-channels", "nextcloud-redis"}
ALLOWED_SERVICES = {"sagetv", "channels-dvr", "docker"}


class SystemService:
    """System-level commands and diagnostics."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _run(cmd: list[str], timeout: int = 30,
                   stdin_data: str | None = None) -> tuple[int, str, str]:
        """Execute a subprocess, optionally feeding stdin (for sudo -S)."""
        logger.debug("Executing: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            input_bytes = (stdin_data + "\n").encode() if stdin_data else None
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_bytes), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", "Command timed out"
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def execute(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatch a system action to the correct handler.

        Actions: info, diagnostics, volume, reboot, shutdown.
        """
        handler = getattr(self, f"_cmd_{action}", None)
        if handler is None:
            logger.warning("Unknown system command: %s", action)
            return {"error": f"Unknown system command '{action}'"}

        async with self._lock:
            try:
                return await handler(payload)
            except Exception as exc:
                logger.exception("System command '%s' failed", action)
                return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------

    async def _cmd_info(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Return static system information."""
        logger.info("Gathering system info")
        info = SystemInfo(
            os=platform.system(),
            hostname=platform.node(),
            cpu=platform.processor() or "unknown",
            memory="unknown",
        )
        return {"status": "ok", "action": "info", **info.to_dict()}

    async def _cmd_diagnostics(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Return runtime diagnostics (CPU, RAM, disk, GPU)."""
        logger.info("Gathering system diagnostics")
        diag = SystemDiagnostics()

        # CPU utilization
        try:
            load = os.getloadavg()
            diag.cpu_percent = load[0] * 100.0 / max(os.cpu_count() or 1, 1)
        except (OSError, AttributeError):
            diag.cpu_percent = 0.0

        # Disk usage
        try:
            usage = shutil.disk_usage("/")
            diag.disk_total_gb = usage.total / (1024 ** 3)
            diag.disk_used_gb = usage.used / (1024 ** 3)
        except Exception:
            pass

        # Memory — best-effort via /proc/meminfo (Linux)
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            meminfo: Dict[str, int] = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
            total = meminfo.get("MemTotal", 0)
            avail = meminfo.get("MemAvailable", 0)
            diag.memory_total_mb = total / 1024.0
            diag.memory_used_mb = (total - avail) / 1024.0
        except Exception:
            pass

        # GPU probe via nvidia-smi
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and stdout:
                parts = stdout.decode().strip().split(",")
                diag.gpu_available = True
                diag.gpu_name = parts[0].strip()
                if len(parts) > 1:
                    diag.gpu_memory_mb = float(parts[1].strip())
        except FileNotFoundError:
            pass

        return {"status": "ok", "action": "diagnostics", **diag.to_dict()}

    async def _cmd_volume(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get or set system volume."""
        level = payload.get("level")
        logger.info("Volume command: level=%s", level)
        # Replace with actual amixer / pactl calls
        return {"status": "ok", "action": "volume", "level": level}

    async def _cmd_reboot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Initiate system reboot."""
        logger.warning("Reboot requested")
        password = payload.get("sudo_password")
        if not password:
            return {"error": "Server password required for reboot"}
        rc, out, err = await self._run(
            ["sudo", "-S", "reboot"], timeout=15, stdin_data=password,
        )
        if rc != 0:
            return {"error": f"Reboot failed: {err.strip()}"}
        return {"status": "ok", "action": "reboot"}

    async def _cmd_shutdown(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Initiate system shutdown."""
        logger.warning("Shutdown requested")
        password = payload.get("sudo_password")
        if not password:
            return {"error": "Server password required for shutdown"}
        rc, out, err = await self._run(
            ["sudo", "-S", "shutdown", "-h", "now"], timeout=15, stdin_data=password,
        )
        if rc != 0:
            return {"error": f"Shutdown failed: {err.strip()}"}
        return {"status": "ok", "action": "shutdown"}

    # ------------------------------------------------------------------
    # Docker / Service Management
    # ------------------------------------------------------------------

    async def _cmd_docker_status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """List running Docker containers."""
        rc, out, err = await self._run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
        )
        if rc != 0:
            return {"error": f"docker ps failed: {err.strip()}"}
        containers = []
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                containers.append({
                    "name": parts[0], "status": parts[1], "image": parts[2],
                })
        return {"status": "ok", "action": "docker_status", "containers": containers}

    async def _cmd_restart_container(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Restart a Docker container (may need sudo)."""
        container = payload.get("container", "")
        if container not in ALLOWED_DOCKER_CONTAINERS:
            return {"error": f"Container '{container}' not in allowlist",
                    "allowed": sorted(ALLOWED_DOCKER_CONTAINERS)}
        password = payload.get("sudo_password")
        if password:
            cmd = ["sudo", "-S", "docker", "restart", container]
            rc, out, err = await self._run(cmd, timeout=60, stdin_data=password)
        else:
            cmd = ["docker", "restart", container]
            rc, out, err = await self._run(cmd, timeout=60)
        if rc != 0:
            return {"error": f"Restart failed: {err.strip()}"}
        return {"status": "ok", "action": "restart_container", "container": container}

    async def _cmd_restart_service(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Restart a systemd service (requires sudo password)."""
        service = payload.get("service", "")
        if service not in ALLOWED_SERVICES:
            return {"error": f"Service '{service}' not in allowlist",
                    "allowed": sorted(ALLOWED_SERVICES)}
        password = payload.get("sudo_password")
        if not password:
            return {"error": "Server password required to restart services"}
        rc, out, err = await self._run(
            ["sudo", "-S", "systemctl", "restart", service],
            timeout=30, stdin_data=password,
        )
        if rc != 0:
            return {"error": f"Restart failed (rc={rc}): {err.strip()}"}
        return {"status": "ok", "action": "restart_service", "service": service}
