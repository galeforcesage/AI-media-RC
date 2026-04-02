"""
registry.py
SQLite-backed device registry per Appendix E.

Handles device CRUD, pairing, lifecycle (stale/expired), and default selection.
"""

from __future__ import annotations
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from .models import Device

logger = logging.getLogger(__name__)

DEFAULT_DEVICE_LIMIT = 15
HARD_DEVICE_LIMIT = 50


class DeviceRegistry:
    """Persistent device store backed by SQLite."""

    def __init__(self, db_path: str = "devices.db", device_limit: int = DEFAULT_DEVICE_LIMIT):
        self.db_path = db_path
        self.device_limit = min(device_limit, HARD_DEVICE_LIMIT)
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                friendly_name TEXT NOT NULL,
                system TEXT NOT NULL,
                ip_address TEXT DEFAULT '',
                platform TEXT DEFAULT 'unknown',
                capabilities TEXT DEFAULT '{}',
                last_seen REAL DEFAULT 0,
                paired_at REAL DEFAULT 0,
                pairing_method TEXT DEFAULT 'manual',
                is_default INTEGER DEFAULT 0
            )
        """)
        self._conn.commit()
        logger.info("Device registry opened: %s (%d device limit)", self.db_path, self.device_limit)

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_device(self, device: Device) -> Device:
        count = self._count()
        if count >= self.device_limit:
            raise ValueError(f"Device limit reached ({self.device_limit}). Delete a device first.")
        now = time.time()
        device.paired_at = now
        device.last_seen = now
        self._conn.execute(
            """INSERT OR REPLACE INTO devices
               (device_id, friendly_name, system, ip_address, platform,
                capabilities, last_seen, paired_at, pairing_method, is_default)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (device.device_id, device.friendly_name, device.system,
             device.ip_address, device.platform,
             json.dumps(device.capabilities),
             device.last_seen, device.paired_at,
             device.pairing_method, int(device.is_default)),
        )
        self._conn.commit()
        logger.info("Device added: %s (%s)", device.device_id, device.friendly_name)
        return device

    def get_device(self, device_id: str) -> Optional[Device]:
        row = self._conn.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            return None
        return Device.from_dict(dict(row))

    def list_devices(self, include_expired: bool = False) -> List[Device]:
        rows = self._conn.execute("SELECT * FROM devices ORDER BY friendly_name").fetchall()
        devices = [Device.from_dict(dict(r)) for r in rows]
        if not include_expired:
            devices = [d for d in devices if not d.is_expired]
        return devices

    def update_device(self, device_id: str, updates: Dict) -> Optional[Device]:
        existing = self.get_device(device_id)
        if not existing:
            return None
        allowed = {"friendly_name", "ip_address", "platform", "capabilities", "is_default"}
        sets = []
        vals = []
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k == "capabilities":
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return existing
        vals.append(device_id)
        self._conn.execute(
            f"UPDATE devices SET {', '.join(sets)} WHERE device_id = ?", vals
        )
        self._conn.commit()
        return self.get_device(device_id)

    def delete_device(self, device_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
        self._conn.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("Device deleted: %s", device_id)
        return deleted

    def touch_device(self, device_id: str) -> None:
        self._conn.execute(
            "UPDATE devices SET last_seen = ? WHERE device_id = ?",
            (time.time(), device_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Default device
    # ------------------------------------------------------------------

    def set_default(self, device_id: str) -> None:
        self._conn.execute("UPDATE devices SET is_default = 0")
        self._conn.execute(
            "UPDATE devices SET is_default = 1 WHERE device_id = ?", (device_id,)
        )
        self._conn.commit()

    def get_default(self) -> Optional[Device]:
        row = self._conn.execute(
            "SELECT * FROM devices WHERE is_default = 1"
        ).fetchone()
        if row is None:
            return None
        return Device.from_dict(dict(row))

    # ------------------------------------------------------------------
    # Pairing helpers
    # ------------------------------------------------------------------

    def pair_from_qr(self, system: str, device_id: str, ip: str, name: str) -> Device:
        platform = device_id.split("-")[1] if "-" in device_id else "unknown"
        device = Device(
            device_id=device_id,
            friendly_name=name,
            system=system,
            ip_address=ip,
            platform=platform,
            pairing_method="qr",
        )
        return self.add_device(device)

    def pair_from_api(self, system: str, client_info: Dict) -> Device:
        client_id = str(client_info.get("id", client_info.get("IP", "")))
        name = client_info.get("name", client_info.get("Name", f"Client {client_id}"))
        ip = client_info.get("ip", client_info.get("IP", ""))
        platform = client_info.get("platform", client_info.get("Platform", "unknown")).lower()
        device_id = Device.generate_id(system, platform)
        device = Device(
            device_id=device_id,
            friendly_name=name,
            system=system,
            ip_address=ip,
            platform=platform,
            pairing_method="api",
        )
        return self.add_device(device)

    def pair_manual(self, system: str, name: str, ip: str, platform: str = "unknown") -> Device:
        device_id = Device.generate_id(system, platform)
        device = Device(
            device_id=device_id,
            friendly_name=name,
            system=system,
            ip_address=ip,
            platform=platform,
            pairing_method="manual",
        )
        return self.add_device(device)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM devices").fetchone()
        return row[0]
