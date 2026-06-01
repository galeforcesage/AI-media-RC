from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from transport.http import _is_internal_override_enabled, _sanitize_query_metadata


def test_internal_override_flag_parsing():
    assert _is_internal_override_enabled("true")
    assert _is_internal_override_enabled("1")
    assert not _is_internal_override_enabled("false")
    assert not _is_internal_override_enabled(None)


def test_planner_keys_removed_without_internal_override():
    metadata = {
        "planner": "openclaw",
        "shadow_planner": "agentloop",
        "request_id": "abc",
    }
    sanitized = _sanitize_query_metadata(metadata, allow_internal_override=False)
    assert sanitized == {"request_id": "abc"}


def test_planner_keys_preserved_with_internal_override():
    metadata = {
        "planner": "openclaw",
        "shadow_planner": "agentloop",
        "request_id": "abc",
    }
    sanitized = _sanitize_query_metadata(metadata, allow_internal_override=True)
    assert sanitized == metadata
