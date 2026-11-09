"""Validation for the narrow, trusted LLM Remote A2UI catalog."""

from __future__ import annotations

from typing import Any, Dict

from services.ui_models import DEFAULT_CATALOG_ID, UI_PROTOCOL


ALLOWED_COMPONENTS = {
    "RecordingList",
    "ConfirmationCard",
    "ErrorNotice",
}
ALLOWED_ACTIONS = {"confirm", "cancel"}
ALLOWED_STYLES = {"default", "primary", "danger"}


class SurfaceValidationError(ValueError):
    """Raised when a surface violates the local catalog contract."""


def validate_surface(message: Dict[str, Any], max_components: int = 100) -> Dict[str, Any]:
    """Validate a surface before transport to the browser."""
    if message.get("protocol") != UI_PROTOCOL:
        raise SurfaceValidationError("Unsupported UI protocol")
    if message.get("type") != "surface":
        raise SurfaceValidationError("Unsupported UI message type")
    if message.get("catalog_id") != DEFAULT_CATALOG_ID:
        raise SurfaceValidationError("Unknown UI catalog")

    components = message.get("components")
    if not isinstance(components, list):
        raise SurfaceValidationError("components must be a list")
    if len(components) > max_components:
        raise SurfaceValidationError("surface exceeds component limit")

    for component in components:
        if not isinstance(component, dict):
            raise SurfaceValidationError("component must be an object")
        name = component.get("component")
        if name not in ALLOWED_COMPONENTS:
            raise SurfaceValidationError(f"Unsupported component: {name}")
        if not isinstance(component.get("props", {}), dict):
            raise SurfaceValidationError("component props must be an object")
        actions = component.get("actions", [])
        if not isinstance(actions, list):
            raise SurfaceValidationError("component actions must be a list")
        for action in actions:
            if not isinstance(action, dict):
                raise SurfaceValidationError("action must be an object")
            if action.get("action") not in ALLOWED_ACTIONS:
                raise SurfaceValidationError("unsupported action")
            if action.get("style", "default") not in ALLOWED_STYLES:
                raise SurfaceValidationError("unsupported action style")
            if not isinstance(action.get("action_id"), str) or not action["action_id"]:
                raise SurfaceValidationError("action_id is required")

    return message
