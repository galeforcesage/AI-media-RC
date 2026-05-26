#!/usr/bin/env python3
"""
re_enrich_dates.py

Backfill `air_date` (and `record_date` when missing) on existing rows in
transcript_index.db by re-querying the MCP servers. Run on the host that has
both the DB and live MCP services.

Usage:
    python3 scripts/re_enrich_dates.py [--db PATH] [--dry-run] [--limit N]
                                       [--channels-url HOST:PORT]
                                       [--sagetv-url HOST:PORT]

Targets rows where `air_date IS NULL` OR `air_date = record_date` (the latter
is the signature of the old buggy fallback that copied the filename-derived
record timestamp into air_date). Sleeps briefly between MCP calls to avoid
hammering the DVR API.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("re_enrich_dates")


def _parse_addr(url: str) -> tuple[str, int]:
    host, _, port = url.partition(":")
    return host or "127.0.0.1", int(port or "0")


async def fetch_metadata(system: str, recording_id: str,
                         channels_url: str, sagetv_url: str) -> dict | None:
    """Mirror of MetadataEnrichmentPipeline.fetch_metadata, minus side effects."""
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
        req = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }) + "\n"
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
        # Unwrap {"success": true, "data": {...}, ...} envelope
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict) and (
            "success" in raw or "ok" in raw
        ):
            return raw["data"]
        return raw if isinstance(raw, dict) else None
    except Exception as e:
        logger.warning("MCP fetch failed for %s/%s: %s", system, recording_id, e)
        return None


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="/home/{username}/AI-media-RC/backend/transcription/transcript_index.db")
    p.add_argument("--channels-url", default="127.0.0.1:8767")
    p.add_argument("--sagetv-url", default="127.0.0.1:8766")
    p.add_argument("--limit", type=int, default=0, help="0 = no limit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sleep", type=float, default=0.1)
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT recording_id, system, air_date, record_date, title
           FROM recordings
           WHERE air_date IS NULL
              OR (record_date IS NOT NULL AND air_date = record_date)
           ORDER BY transcribed_at DESC NULLS LAST"""
    ).fetchall()

    if args.limit:
        rows = rows[:args.limit]

    logger.info("Found %d candidate rows to re-enrich", len(rows))
    updated = skipped = unchanged = failed = 0

    for r in rows:
        rid, system = r["recording_id"], r["system"]
        meta = await fetch_metadata(system, rid, args.channels_url, args.sagetv_url)
        if not meta:
            failed += 1
            continue

        # Apply same promotion rules as enrichment.py
        new_air = meta.get("original_air_epoch") or meta.get("air_date")
        new_rec = meta.get("record_date") or r["record_date"]

        # air_date in metadata may be a display string ("Thu May 7"); skip those
        if isinstance(new_air, str):
            new_air = None
        if isinstance(new_rec, str):
            new_rec = None

        if not new_air and not new_rec:
            skipped += 1
            continue

        old_air, old_rec = r["air_date"], r["record_date"]
        # Only update fields that change, but require at least one real change
        upd_air = new_air if (new_air and new_air != old_air) else old_air
        upd_rec = new_rec if (new_rec and new_rec != old_rec) else old_rec

        if upd_air == old_air and upd_rec == old_rec:
            unchanged += 1
            continue

        logger.info("[%s] %s: air_date %s -> %s ; record_date %s -> %s",
                    system, rid[:60], old_air, upd_air, old_rec, upd_rec)
        if not args.dry_run:
            conn.execute(
                "UPDATE recordings SET air_date = ?, record_date = ?, "
                "updated_at = strftime('%s','now') WHERE recording_id = ?",
                (upd_air, upd_rec, rid),
            )
            conn.commit()
        updated += 1
        await asyncio.sleep(args.sleep)

    logger.info("Done. updated=%d unchanged=%d skipped=%d failed=%d (dry_run=%s)",
                updated, unchanged, skipped, failed, args.dry_run)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
