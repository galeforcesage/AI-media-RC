#!/usr/bin/env python3
"""
re_enrich_metadata.py

Backfill missing transcript index recording metadata by re-querying MCP servers.

Targets rows where one or more core metadata fields are missing:
  - episode_title
  - season
  - episode
  - channel

Also updates additional fields when MCP provides better values:
  - channel_number, genre, description, rating, source_id
  - air_date, record_date

Usage:
  python3 scripts/re_enrich_metadata.py [--db PATH] [--dry-run] [--limit N]
                                       [--channels-url HOST:PORT]
                                       [--sagetv-url HOST:PORT]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("re_enrich_metadata")


def _parse_addr(url: str) -> tuple[str, int]:
    host, _, port = url.partition(":")
    return host or "127.0.0.1", int(port or "0")


def _to_epoch(value: Any) -> Optional[int]:
    """Convert supported date/time values to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        iv = int(value)
        return iv if iv > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            iv = int(text)
            return iv if iv > 0 else None
        # Handle common ISO-8601 strings.
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def _normalize_sagetv_metadata(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize SageTV API response into the recording metadata shape."""
    data = raw.get("data") or raw
    airing = data.get("Airing") or data.get("airing") or {}
    show = airing.get("Show") or airing.get("show") or {}
    channel = airing.get("Channel") or airing.get("channel") or {}

    return {
        "title": show.get("Title") or show.get("title") or "",
        "episode_title": show.get("EpisodeTitle") or show.get("episodeTitle"),
        "season": show.get("SeasonNumber") or show.get("seasonNumber"),
        "episode": show.get("EpisodeNumber") or show.get("episodeNumber"),
        "genre": show.get("Category") or show.get("category") or show.get("Genre") or [],
        "channel": channel.get("CallSign") or channel.get("callSign"),
        "channel_number": channel.get("ChannelNumber") or channel.get("channelNumber"),
        "air_date": airing.get("StartTime") or airing.get("startTime"),
        "record_date": data.get("StartTime") or data.get("startTime"),
        "description": show.get("Description") or show.get("description"),
        "rating": show.get("Rated") or show.get("ParentalRating") or show.get("rated"),
        "source_id": show.get("ExternalID") or show.get("externalID"),
    }


async def fetch_metadata(
    system: str,
    recording_id: str,
    channels_url: str,
    sagetv_url: str,
) -> Optional[Dict[str, Any]]:
    if system == "sagetv":
        host, port = _parse_addr(sagetv_url)
        tool = "sagetv_get_recording"
        parts = recording_id.rsplit("-", 2)
        media_file_id = parts[-2] if len(parts) >= 3 else recording_id
        arguments = {"media_file_id": media_file_id}
    elif system == "channelsdvr":
        host, port = _parse_addr(channels_url)
        tool = "channels_get_recording"
        arguments = {"recording_id": recording_id}
    else:
        return None

    try:
        reader, writer = await asyncio.open_connection(host, port)
        req = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        ) + "\n"
        writer.write(req.encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=10.0)
        writer.close()
        await writer.wait_closed()
        if not line:
            return None

        resp = json.loads(line.decode())
        content = resp.get("result", {}).get("content", [])
        if not (content and content[0].get("type") == "text"):
            return None

        raw = json.loads(content[0]["text"])
        if system == "sagetv":
            return _normalize_sagetv_metadata(raw)

        # Unwrap envelope from channels MCP tools.
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict) and (
            "success" in raw or "ok" in raw
        ):
            raw = raw["data"]
        return raw if isinstance(raw, dict) else None
    except Exception as exc:
        logger.warning("MCP fetch failed for %s/%s: %s", system, recording_id, exc)
        return None


def _normalize_genre(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _normalize_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _derive_episode_title_fallback(system: str, title: Any) -> Optional[str]:
    """Conservative fallback for one-off programs that have no episodic metadata.

    Only applies to Channels rows, where titles are usually human readable.
    """
    if system != "channelsdvr":
        return None
    return _normalize_text(title)


def _parse_sxxexx(value: Any) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Extract season/episode/episode_title from strings like 'Show S02E17 Name ...'."""
    text = _normalize_text(value)
    if not text:
        return None, None, None

    m = re.search(r"\bS(\d{1,4})E(\d{1,4})\b", text, flags=re.IGNORECASE)
    if not m:
        return None, None, None

    season = _normalize_int(m.group(1))
    episode = _normalize_int(m.group(2))

    # Heuristic: use the text after SxxExx as episode title, dropping common timestamp tails.
    tail = text[m.end():].strip(" -._")
    tail = re.sub(r"\s+\d{4}-\d{2}-\d{2}-\d{3,4}$", "", tail).strip(" -._")
    tail = re.sub(r"\s{2,}", " ", tail)
    episode_title = _normalize_text(tail)

    return season, episode, episode_title


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default="/home/USER_HOME/AI-media-RC/backend/transcription/transcript_index.db",
    )
    parser.add_argument("--channels-url", default="127.0.0.1:8767")
    parser.add_argument("--sagetv-url", default="127.0.0.1:8766")
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.1)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT
            recording_id,
            system,
            title,
            episode_title,
            season,
            episode,
            channel,
            channel_number,
            genre,
            description,
            rating,
            source_id,
            air_date,
            record_date
        FROM recordings
        WHERE (episode_title IS NULL OR TRIM(episode_title) = '')
           OR season IS NULL
           OR episode IS NULL
           OR (channel IS NULL OR TRIM(channel) = '')
        ORDER BY transcribed_at DESC
        """
    ).fetchall()

    if args.limit:
        rows = rows[: args.limit]

    logger.info("Found %d candidate rows to re-enrich", len(rows))
    updated = unchanged = failed = 0

    for row in rows:
        rid = row["recording_id"]
        system = row["system"]

        metadata = await fetch_metadata(system, rid, args.channels_url, args.sagetv_url)
        if not metadata:
            failed += 1
            continue

        parsed_season = parsed_episode = None
        parsed_episode_title = None
        for candidate in (metadata.get("title"), row["title"], rid):
            s_val, e_val, ep_title = _parse_sxxexx(candidate)
            if parsed_season is None and s_val is not None:
                parsed_season = s_val
            if parsed_episode is None and e_val is not None:
                parsed_episode = e_val
            if parsed_episode_title is None and ep_title:
                parsed_episode_title = ep_title
            if parsed_season is not None and parsed_episode is not None and parsed_episode_title:
                break

        episode_title = _normalize_text(metadata.get("episode_title")) or parsed_episode_title
        if not episode_title:
            episode_title = _derive_episode_title_fallback(system, row["title"])

        new_values: Dict[str, Any] = {
            "episode_title": episode_title,
            "season": _normalize_int(metadata.get("season")) or parsed_season,
            "episode": _normalize_int(metadata.get("episode")) or parsed_episode,
            "channel": _normalize_text(metadata.get("channel")),
            "channel_number": _normalize_text(metadata.get("channel_number")),
            "genre": _normalize_genre(metadata.get("genre")),
            "description": _normalize_text(metadata.get("description")),
            "rating": _normalize_text(metadata.get("rating")),
            "source_id": _normalize_text(metadata.get("source_id")),
            "air_date": _to_epoch(metadata.get("original_air_epoch") or metadata.get("air_date")),
            "record_date": _to_epoch(metadata.get("record_date")),
        }

        changed_cols: Dict[str, Any] = {}
        for col, new_val in new_values.items():
            # Keep existing values unless we have a concrete new value.
            if new_val is None:
                continue
            old_val = row[col] if col in row.keys() else None
            if old_val != new_val:
                changed_cols[col] = new_val

        if not changed_cols:
            unchanged += 1
            continue

        logger.info(
            "[%s] %s updated fields: %s",
            system,
            rid[:80],
            ", ".join(sorted(changed_cols.keys())),
        )

        if not args.dry_run:
            assignments = ", ".join([f"{k} = ?" for k in changed_cols.keys()])
            values = list(changed_cols.values())
            values.extend([rid])
            conn.execute(
                f"UPDATE recordings SET {assignments}, updated_at = strftime('%s','now') WHERE recording_id = ?",
                values,
            )
            conn.commit()
        updated += 1
        await asyncio.sleep(args.sleep)

    logger.info("Done. updated=%d unchanged=%d failed=%d (dry_run=%s)", updated, unchanged, failed, args.dry_run)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())