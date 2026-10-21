"""Deterministic compilers from domain data to trusted UI surfaces."""

from __future__ import annotations

from html import unescape
import re
from typing import Any, Dict, Iterable

from services.ui_models import UIAction, UIComponent, UISurface
from services.a2ui_validator import validate_surface


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _text(value: Any, limit: int = 500) -> str:
    """Normalize untrusted backend text before it reaches a component."""
    text = unescape(str(value or ""))
    text = _CONTROL_CHARS.sub("", text).strip()
    return text[:limit]


def recording_list_surface(
    recordings: Iterable[Dict[str, Any]],
    text_fallback: str,
    title: str = "Recordings",
) -> Dict[str, Any]:
    """Compile normalized DVR records into a RecordingList surface."""
    items = []
    for record in recordings:
        items.append({
            "title": _text(record.get("title"), 200),
            "episode_title": _text(record.get("episode_title"), 200),
            "season_episode": _text(record.get("season_episode"), 30),
            "air_date": _text(
                record.get("air_date") or record.get("start_time"), 80
            ),
            "channel": _text(
                record.get("channel") or record.get("channel_name"), 100
            ),
            "status": _text(record.get("status") or "available", 40),
            "watched": record.get("watched", record.get("is_watched")),
            "system": _text(record.get("system") or record.get("source"), 40),
        })

    message = UISurface(
        title=_text(title, 120),
        components=[
            UIComponent(
                component="RecordingList",
                props={"items": items, "count": len(items)},
            )
        ],
        text_fallback=text_fallback,
    ).to_message()
    return validate_surface(message)


def confirmation_surface(
    pending_action_id: str,
    message: str,
    expires_at: float,
) -> Dict[str, Any]:
    """Compile an opaque pending action into a ConfirmationCard."""
    surface = UISurface(
        title="Confirmation required",
        components=[
            UIComponent(
                component="ConfirmationCard",
                props={
                    "message": _text(message, 500),
                    "operation": "privileged action",
                    "expires_at": expires_at,
                },
                actions=[
                    UIAction(
                        action_id=pending_action_id,
                        label="Confirm",
                        action="confirm",
                        style="danger",
                    ),
                    UIAction(
                        action_id=pending_action_id,
                        label="Cancel",
                        action="cancel",
                    ),
                ],
            )
        ],
        text_fallback=message,
    ).to_message()
    return validate_surface(surface)
