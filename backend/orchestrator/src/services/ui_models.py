"""Trusted dynamic-surface models for the local conversational UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List
import uuid


UI_PROTOCOL = "a2ui-v0.9.1"
DEFAULT_CATALOG_ID = "urn:llm-remote:a2ui:catalog:v1"


@dataclass(frozen=True)
class UIAction:
    """An action selected from the server-owned action registry."""

    action_id: str
    label: str
    action: str
    style: str = "default"

    def to_dict(self) -> Dict[str, str]:
        return {
            "action_id": self.action_id,
            "label": self.label,
            "action": self.action,
            "style": self.style,
        }


@dataclass(frozen=True)
class UIComponent:
    """One catalog component and its validated properties."""

    component: str
    props: Dict[str, Any] = field(default_factory=dict)
    actions: List[UIAction] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "component": self.component,
            "props": self.props,
        }
        if self.actions:
            data["actions"] = [action.to_dict() for action in self.actions]
        return data


@dataclass(frozen=True)
class UISurface:
    """A deterministic surface compiled from authoritative domain data."""

    title: str
    components: List[UIComponent]
    text_fallback: str = ""
    catalog_id: str = DEFAULT_CATALOG_ID
    surface_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_message(self) -> Dict[str, Any]:
        return {
            "protocol": UI_PROTOCOL,
            "type": "surface",
            "catalog_id": self.catalog_id,
            "surface_id": self.surface_id,
            "title": self.title,
            "components": [component.to_dict() for component in self.components],
            "text_fallback": self.text_fallback,
        }
