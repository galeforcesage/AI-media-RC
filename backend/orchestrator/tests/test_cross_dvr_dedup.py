from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from services.recording_dedup import dedupe_cross_dvr_recordings


def _recording(system: str, recording_id: str, *, watched: bool = False) -> dict:
    return {
        "id": recording_id,
        "title": "Elsbeth",
        "episode_title": "Death of a Playwright",
        "season_episode": "S04E02",
        "record_date": 1792112460,
        "watched": watched,
        "system": system,
    }


def test_merges_same_episode_recorded_by_both_dvrs():
    rows = dedupe_cross_dvr_recordings([
        _recording("sagetv", "sage-1", watched=True),
        _recording("channelsdvr", "channels-1", watched=False),
    ])

    assert len(rows) == 1
    assert rows[0]["id"] == "sage-1"
    assert rows[0]["systems"] == ["sagetv", "channelsdvr"]
    assert rows[0]["watched"] is False


def test_keeps_repeat_recordings_from_the_same_dvr():
    rows = dedupe_cross_dvr_recordings([
        _recording("sagetv", "sage-1"),
        _recording("sagetv", "sage-2"),
    ])

    assert len(rows) == 2
    assert [row["id"] for row in rows] == ["sage-1", "sage-2"]
