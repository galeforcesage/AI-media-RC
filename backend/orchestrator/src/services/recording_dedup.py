"""Cross-DVR recording identity and merge helpers."""

from __future__ import annotations

import re
from typing import Any


def _normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _recording_epoch(recording: dict) -> int | None:
    try:
        value = recording.get("record_date")
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _same_cross_dvr_episode(left: dict, right: dict) -> bool:
    if not left.get("system") or not right.get("system"):
        return False
    if left["system"] == right["system"]:
        return False
    if _normalized_text(left.get("title")) != _normalized_text(right.get("title")):
        return False

    left_episode = _normalized_text(left.get("episode_title"))
    right_episode = _normalized_text(right.get("episode_title"))
    left_se = _normalized_text(left.get("season_episode"))
    right_se = _normalized_text(right.get("season_episode"))
    if left_episode and right_episode:
        if left_episode != right_episode:
            return False
    elif left_se and right_se:
        if left_se != right_se:
            return False
    else:
        return False

    left_epoch = _recording_epoch(left)
    right_epoch = _recording_epoch(right)
    return (
        left_epoch is not None
        and right_epoch is not None
        and abs(left_epoch - right_epoch) <= 300
    )


def dedupe_cross_dvr_recordings(recordings: list[dict]) -> list[dict]:
    """Merge equivalent episodes only when they came from different DVRs."""
    merged: list[dict] = []
    for recording in recordings:
        row = dict(recording)
        row["systems"] = [row["system"]] if row.get("system") else []

        match = next(
            (existing for existing in merged if _same_cross_dvr_episode(existing, row)),
            None,
        )
        if match is None:
            merged.append(row)
            continue

        for system in row["systems"]:
            if system not in match["systems"]:
                match["systems"].append(system)

        watched_values = [match.get("watched"), row.get("watched")]
        if False in watched_values:
            match["watched"] = False
        elif all(value is True for value in watched_values):
            match["watched"] = True

    return merged
