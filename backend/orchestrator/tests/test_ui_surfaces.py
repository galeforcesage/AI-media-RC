from __future__ import annotations

import sys
from pathlib import Path
import time

from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from transport import http
from services.a2ui_validator import SurfaceValidationError, validate_surface
from services.pending_actions import PendingActionStore
from services.ui_surface import confirmation_surface, recording_list_surface
from utils.config import validate_config


def test_recording_surface_sanitizes_and_uses_trusted_catalog():
    message = recording_list_surface(
        [{
            "title": "Example\x00 Show",
            "episode_title": "<script>alert(1)</script>",
            "air_date": "2026-10-21",
            "watched": False,
        }],
        text_fallback='1. "Example Show"',
    )

    assert message["protocol"] == "a2ui-v0.9.1"
    assert message["catalog_id"] == "urn:llm-remote:a2ui:catalog:v1"
    item = message["components"][0]["props"]["items"][0]
    assert item["title"] == "Example Show"
    assert item["episode_title"] == "<script>alert(1)</script>"


def test_confirmation_surface_exposes_only_opaque_action_id():
    message = confirmation_surface(
        pending_action_id="opaque-id",
        message="Restart the service?",
        expires_at=time.time() + 300,
    )

    component = message["components"][0]
    assert component["component"] == "ConfirmationCard"
    assert {action["action"] for action in component["actions"]} == {
        "confirm", "cancel"
    }
    assert all(action["action_id"] == "opaque-id" for action in component["actions"])
    assert "args" not in component
    assert "linux_restart_service" not in str(message)


def test_validator_rejects_unknown_component():
    message = recording_list_surface([], text_fallback="No recordings found.")
    message["components"][0]["component"] = "RawHtml"
    try:
        validate_surface(message)
    except SurfaceValidationError:
        pass
    else:
        raise AssertionError("unknown component should be rejected")


def test_pending_action_is_owner_bound_and_single_use():
    store = PendingActionStore(ttl_seconds=300)
    pending = store.create(
        owner_key="owner-a",
        tool="linux_reboot_server",
        args={},
    )

    assert store.consume(pending.action_id, "owner-b") is None
    consumed = store.consume(pending.action_id, "owner-a")
    assert consumed is not None
    assert consumed.tool == "linux_reboot_server"
    assert store.consume(pending.action_id, "owner-a") is None


def test_ui_config_rejects_model_authored_layout():
    try:
        validate_config({
            "ui": {
                "enabled": True,
                "allow_model_layout": True,
            }
        })
    except ValueError as exc:
        assert "allow_model_layout" in str(exc)
    else:
        raise AssertionError("model-authored layout must remain disabled")


def test_http_confirmation_replaces_raw_tool_and_arguments(monkeypatch):
    class FakeOrchestrator:
        config = {
            "ui": {
                "enabled": True,
                "max_components_per_surface": 100,
            }
        }
        pending_actions = PendingActionStore(ttl_seconds=300)

    monkeypatch.setattr(http, "_orchestrator", FakeOrchestrator())
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/query",
        "headers": [(b"cookie", b"app_session=session-secret")],
    })
    result = http._prepare_ui_result(
        {
            "status": "confirmation_required",
            "response": "Restart the service?",
            "confirmation": {
                "tool": "linux_restart_service",
                "args": {"service_name": "sagetv"},
            },
        },
        request,
        ["sagetv"],
    )

    serialized = str(result)
    assert "pending_action_id" in result["confirmation"]
    assert "linux_restart_service" not in serialized
    assert "service_name" not in serialized
    assert "sagetv" not in serialized


def test_http_compiles_episode_metadata_into_recording_surface(monkeypatch):
    class FakeOrchestrator:
        config = {
            "ui": {
                "enabled": True,
                "max_components_per_surface": 100,
            }
        }

    monkeypatch.setattr(http, "_orchestrator", FakeOrchestrator())
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/query",
        "headers": [],
    })
    result = http._prepare_ui_result(
        {
            "status": "ok",
            "llm_response": '1. "Example"',
            "episode_meta": [{
                "title": "Example",
                "episode_title": "Pilot",
                "channel": "10.1",
                "system": "channelsdvr",
            }],
        },
        request,
        ["channelsdvr"],
    )

    surface = result["ui_messages"][0]
    assert surface["components"][0]["component"] == "RecordingList"
    assert surface["components"][0]["props"]["items"][0]["title"] == "Example"
