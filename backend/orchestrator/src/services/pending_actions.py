"""Opaque, session-bound storage for privileged confirmation actions."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import time
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class PendingAction:
    action_id: str
    owner_key: str
    tool: str
    args: Dict[str, Any]
    systems: list[str] | None
    created_at: float
    expires_at: float


class PendingActionStore:
    """In-memory, single-use pending action registry."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = max(30, int(ttl_seconds))
        self._actions: Dict[str, PendingAction] = {}

    def create(
        self,
        owner_key: str,
        tool: str,
        args: Dict[str, Any],
        systems: list[str] | None = None,
    ) -> PendingAction:
        self._purge_expired()
        now = time.time()
        pending = PendingAction(
            action_id=secrets.token_urlsafe(24),
            owner_key=owner_key,
            tool=tool,
            args=dict(args),
            systems=list(systems) if systems else None,
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._actions[pending.action_id] = pending
        return pending

    def consume(self, action_id: str, owner_key: str) -> Optional[PendingAction]:
        self._purge_expired()
        pending = self._actions.get(action_id)
        if pending is None or not secrets.compare_digest(pending.owner_key, owner_key):
            return None
        self._actions.pop(action_id, None)
        return pending

    def cancel(self, action_id: str, owner_key: str) -> bool:
        return self.consume(action_id, owner_key) is not None

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [
            action_id
            for action_id, pending in self._actions.items()
            if pending.expires_at <= now
        ]
        for action_id in expired:
            self._actions.pop(action_id, None)
