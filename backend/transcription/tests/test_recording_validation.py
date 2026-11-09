from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import discovery
from src.discovery import SageTVRecordingValidator


def test_sagetv_validator_rejects_live_tv_airing(monkeypatch):
    async def call_tool(host, port, tool_name, arguments):
        assert tool_name == "sagetv_get_airing"
        assert arguments == {"airing_id": "15958319"}
        return {
            "success": True,
            "data": {"Airing": {"IsNotManualOrFavorite": True}},
        }

    monkeypatch.setattr(discovery, "_mcp_call_tool", call_tool)
    validator = SageTVRecordingValidator()

    accepted = asyncio.run(
        validator(
            "/recordings/NFLFootball-DetroitLionsatMiamiDolphins-15958319-1.mpg"
        )
    )

    assert accepted is False


def test_sagetv_validator_accepts_favorite_recording(monkeypatch):
    async def call_tool(host, port, tool_name, arguments):
        return {
            "success": True,
            "data": {"Airing": {"IsNotManualOrFavorite": False}},
        }

    monkeypatch.setattr(discovery, "_mcp_call_tool", call_tool)
    validator = SageTVRecordingValidator()

    accepted = asyncio.run(
        validator(
            "/recordings/AnimalControl-S05E06-BakedChickensandRatDogs-15722036-0.mpg"
        )
    )

    assert accepted is True


def test_sagetv_validator_rechecks_live_tv_that_becomes_recording(monkeypatch):
    states = iter([True, False])

    async def call_tool(host, port, tool_name, arguments):
        return {
            "success": True,
            "data": {"Airing": {"IsNotManualOrFavorite": next(states)}},
        }

    monkeypatch.setattr(discovery, "_mcp_call_tool", call_tool)
    validator = SageTVRecordingValidator(negative_ttl=0)
    path = "/recordings/Show-12345-0.mpg"

    assert asyncio.run(validator(path)) is False
    assert asyncio.run(validator(path)) is True


def test_sagetv_validator_allows_unstructured_library_filename(monkeypatch):
    async def call_tool(*args, **kwargs):
        raise AssertionError("MCP should not be called for library filenames")

    monkeypatch.setattr(discovery, "_mcp_call_tool", call_tool)
    validator = SageTVRecordingValidator()

    assert asyncio.run(validator("/recordings/HomeVideo.mp4")) is True
