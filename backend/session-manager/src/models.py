"""
models.py
Data models for the Unified Session Manager.

Device registry, session resolution, playback context per Appendix E.
"""

from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ------------------------------------------------------------------
# Device
# ------------------------------------------------------------------

@dataclass
class Device:
    device_id: str
    friendly_name: str
    system: str  # "sagetv" | "channelsdvr"
    ip_address: str = ""
    platform: str = "unknown"  # shield, chromecast, miniclient, browser, androidtv, pc, pi
    capabilities: Dict[str, bool] = field(default_factory=lambda: {
        "supports_seek": True,
        "supports_volume": True,
        "supports_commercial_skip": False,
        "supports_playback_speed": False,
    })
    last_seen: float = 0.0
    paired_at: float = 0.0
    pairing_method: str = "manual"  # qr, api, manual
    is_default: bool = False

    @staticmethod
    def generate_id(system: str, platform: str) -> str:
        short = uuid.uuid4().hex[:6]
        return f"{system}-{platform}-{short}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Device:
        caps = d.get("capabilities")
        if isinstance(caps, str):
            caps = json.loads(caps)
        return cls(
            device_id=d["device_id"],
            friendly_name=d["friendly_name"],
            system=d["system"],
            ip_address=d.get("ip_address", ""),
            platform=d.get("platform", "unknown"),
            capabilities=caps or {},
            last_seen=d.get("last_seen", 0.0),
            paired_at=d.get("paired_at", 0.0),
            pairing_method=d.get("pairing_method", "manual"),
            is_default=bool(d.get("is_default", False)),
        )

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_seen) > 30 * 86400

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_seen) > 90 * 86400


# ------------------------------------------------------------------
# Session
# ------------------------------------------------------------------

@dataclass
class PlaybackSession:
    device_id: str
    session_id: str
    system: str
    client_id: str = ""
    media_id: str = ""
    title: str = ""
    episode: str = ""
    position: float = 0.0
    duration: float = 0.0
    state: str = "unknown"  # playing, paused, stopped, idle
    commercial_markers: List[float] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------
# Playback context (injected into LLM prompt)
# ------------------------------------------------------------------

@dataclass
class PlaybackContext:
    device_id: str
    device_name: str
    system: str
    session: Optional[PlaybackSession] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "system": self.system,
        }
        if self.session:
            d["session"] = self.session.to_dict()
        if self.error:
            d["error"] = self.error
        return d
