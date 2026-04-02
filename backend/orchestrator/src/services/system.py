"""
system.py
System diagnostics and control service.
Provides CPU, RAM, disk, GPU availability, and system-level commands
such as volume, reboot, and shutdown.
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


class SystemService:
    """System-level commands and diagnostics."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

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
        # In production: os.system("sudo reboot")
        return {"status": "ok", "action": "reboot"}

    async def _cmd_shutdown(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Initiate system shutdown."""
        logger.warning("Shutdown requested")
        # In production: os.system("sudo shutdown -h now")
        return {"status": "ok", "action": "shutdown"}
