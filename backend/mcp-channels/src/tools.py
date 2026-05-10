"""
tools.py
Complete MCP tool registry for Channels DVR.

Each tool maps to a Channels DVR REST API endpoint per Appendix B / Appendix G.
Tools are namespaced with channels_ prefix.
"""

from __future__ import annotations
import datetime
import enum
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Safety levels
# ------------------------------------------------------------------

class Safety(str, enum.Enum):
    SAFE = "SAFE"
    CONFIRM = "CONFIRM"
    DANGEROUS = "DANGEROUS"
    OWNER = "OWNER"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _ok(data: Any = None, message: str = "OK") -> Dict[str, Any]:
    r: Dict[str, Any] = {"success": True, "message": message}
    if data is not None:
        r["data"] = data
    return r


def _fail(error: str, message: str) -> Dict[str, Any]:
    return {"success": False, "error": error, "message": message}


def _epoch_to_readable(epoch_sec: int) -> str:
    """Convert epoch seconds to a human-readable date string."""
    try:
        dt = datetime.datetime.fromtimestamp(epoch_sec)
        return dt.strftime("%Y-%m-%d %I:%M %p")
    except (ValueError, OSError, TypeError):
        return ""


def _date_str_to_epoch(date_str: str, end_of_day: bool = False) -> int:
    """Convert YYYY-MM-DD to epoch seconds."""
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def _enrich_channels_recording(rec: Dict) -> Dict:
    """Extract only LLM-relevant fields from a Channels DVR recording.

    The raw DVR record is ~5KB (commercials, signal stats, raw EPG, etc.)
    which blows past the agent's 2000-char truncation limit.  Return a
    compact dict so the LLM can see all results at once.
    """
    if not isinstance(rec, dict):
        return rec
    airing = rec.get("Airing") or {}
    air_time = airing.get("Time") or rec.get("CreatedAt") or 0
    season = airing.get("SeasonNumber")
    episode = airing.get("EpisodeNumber")
    se = f"S{season:02d}E{episode:02d}" if isinstance(season, int) and isinstance(episode, int) else ""

    dt = datetime.datetime.fromtimestamp(int(air_time)) if air_time else None
    # Parse OriginalDate (YYYY-MM-DD) into epoch for downstream consumers
    # (transcription enrichment, sidecars). Display string stays under
    # `original_date`; the parsed epoch goes under `original_air_epoch`.
    original_date_str = airing.get("OriginalDate", "")
    original_air_epoch = None
    if isinstance(original_date_str, str) and len(original_date_str) >= 10:
        try:
            original_air_epoch = int(datetime.datetime.strptime(
                original_date_str[:10], "%Y-%m-%d"
            ).timestamp())
        except (ValueError, OSError):
            original_air_epoch = None
    enriched = {
        "id": rec.get("ID", ""),
        "title": airing.get("Title", ""),
        "episode_title": airing.get("EpisodeTitle", ""),
        "season_episode": se,
        "channel": airing.get("Channel", ""),
        "recorded": _epoch_to_readable(int(air_time)) if air_time else "",
        "air_date": dt.strftime("%a %b %-d") if dt else "",
        "original_date": original_date_str,
        # Raw epoch fields for non-display consumers (transcription enrichment).
        # `record_date` = when the DVR captured the airing (Airing.Time / CreatedAt).
        # `original_air_epoch` = when the episode originally aired (OriginalDate).
        "record_date": int(air_time) if air_time else None,
        "original_air_epoch": original_air_epoch,
        "duration_min": round(rec.get("Duration", 0) / 60, 1),
        "description": airing.get("FullSummary", "") or airing.get("Summary", ""),
        "genres": airing.get("Genres", []),
        "image": airing.get("Image", ""),
        "cast": airing.get("Cast", []),
        "content_rating": airing.get("ContentRating", ""),
        "watched": bool(rec.get("Watched") or rec.get("PlayedAt")),
        "path": rec.get("Path", ""),
    }
    if rec.get("Deleted"):
        enriched["status"] = "watched_and_removed"
    else:
        enriched["status"] = "available"
    return enriched


def _enrich_channels_recordings(data: Any) -> Any:
    """Enrich a list of Channels DVR recordings with readable dates."""
    if isinstance(data, list):
        return [_enrich_channels_recording(r) for r in data]
    return data


# ==================================================================
# Playback tool handlers — routed through bridge APK
# ==================================================================

async def _bridge_cmd(bridge, method: str, path: str, body=None, device: str = "") -> Dict:
    """Send a command to a device via the bridge APK."""
    if bridge is None:
        return _fail("no_bridge", "Bridge manager not available")
    dev = bridge.get_device(device)
    if dev is None:
        names = list(bridge.connected_devices.keys())
        if not names:
            return _fail("no_device", "No Channels playback device connected. Ensure a Bridge APK (Android TV) is running or an Apple TV has Channels open.")
        return _fail("device_not_found", f"Device '{device}' not found. Connected: {names}")
    result = await dev.send_command(method, path, body)
    status = result.get("status", 500)
    resp_body = result.get("body", {})
    if status in (200, 201, 204):
        return _ok(data=resp_body, message="OK")
    error_msg = resp_body.get("error", f"HTTP {status}") if isinstance(resp_body, dict) else str(resp_body)
    return _fail("device_error", error_msg)


async def _get_playback_status(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "GET", "/api/status", device=device)
    # Enrich with recording duration from DVR server
    if result.get("success") and isinstance(result.get("data"), dict):
        np = result["data"].get("now_playing", {})
        title = np.get("title", "")
        ep_title = np.get("episode_title", "")
        if title and client:
            try:
                recs = await client.get_recordings()
                for rec in recs:
                    a = rec.get("Airing", {})
                    if a.get("Title") == title and (not ep_title or a.get("EpisodeTitle") == ep_title):
                        result["data"]["duration"] = rec.get("Duration", 0)
                        break
            except Exception:
                pass
    return result


async def _pause_playback(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/pause", device=device)
    if result.get("success"):
        result["message"] = "Playback paused"
    return result


async def _resume_playback(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/resume", device=device)
    if result.get("success"):
        result["message"] = "Playback resumed"
    return result


async def _toggle_pause(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/toggle_pause", device=device)
    if result.get("success"):
        result["message"] = "Play/pause toggled"
    return result


async def _stop_playback(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/stop", device=device)
    if result.get("success"):
        result["message"] = "Playback stopped"
    return result


async def _seek_relative(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    seconds = args.get("seconds", 0)
    result = await _bridge_cmd(bridge, "POST", f"/api/seek/{seconds}", device=device)
    if result.get("success"):
        result["message"] = f"Seeked {seconds}s"
    return result


async def _seek_forward(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/seek_forward", device=device)
    if result.get("success"):
        result["message"] = "Seeked forward"
    return result


async def _seek_backward(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/seek_backward", device=device)
    if result.get("success"):
        result["message"] = "Seeked backward"
    return result


async def _skip_commercial(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/skip_forward", device=device)
    if result.get("success"):
        result["message"] = "Skipped to next commercial marker"
    return result


async def _previous_commercial(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/skip_backward", device=device)
    if result.get("success"):
        result["message"] = "Returned to previous commercial marker"
    return result


async def _toggle_mute(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/toggle_mute", device=device)
    if result.get("success"):
        result["message"] = "Mute toggled"
    return result


async def _toggle_cc(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/toggle_cc", device=device)
    if result.get("success"):
        result["message"] = "Closed captions toggled"
    return result


async def _play_channel(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    channel = args.get("channel_number", "")
    if not channel:
        return _fail("missing_param", "channel_number is required")
    result = await _bridge_cmd(bridge, "POST", f"/api/play/channel/{channel}", device=device)
    if result.get("success"):
        result["message"] = f"Playing channel {channel}"
    return result


async def _play_recording(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    rec_id = args.get("recording_id", "")
    if not rec_id:
        return _fail("missing_param", "recording_id is required")
    result = await _bridge_cmd(bridge, "POST", f"/api/play/recording/{rec_id}", device=device)
    if result.get("success"):
        result["message"] = f"Playing recording {rec_id}"
    return result


async def _channel_up(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/channel_up", device=device)
    if result.get("success"):
        result["message"] = "Channel up"
    return result


async def _channel_down(client, args: Dict, bridge=None) -> Dict:
    device = args.get("device", "")
    result = await _bridge_cmd(bridge, "POST", "/api/channel_down", device=device)
    if result.get("success"):
        result["message"] = "Channel down"
    return result


async def _get_bridge_devices(client, args: Dict, bridge=None) -> Dict:
    if bridge is None:
        return _fail("no_bridge", "Bridge manager not available")
    devices = bridge.connected_devices
    if not devices:
        return _ok(data=[], message="No bridge devices connected")
    return _ok(data=list(devices.values()), message=f"{len(devices)} bridge device(s) connected")


# ==================================================================
# Query tool handlers
# ==================================================================

async def _get_now_playing(client, args: Dict, bridge=None) -> Dict:
    sessions = await client.get_sessions()
    return _ok(data=sessions, message=f"{len(sessions)} active sessions")


async def _get_recordings(client, args: Dict, bridge=None) -> Dict:
    recordings = await client.get_recordings()
    limit = args.get("limit", 50)
    return _ok(data=_enrich_channels_recordings(recordings[:limit]), message=f"{len(recordings)} recordings total, returning {min(limit, len(recordings))}")


async def _get_recording(client, args: Dict, bridge=None) -> Dict:
    """Look up a single recording by its DVR ID (or filename stem)."""
    rec_id = args.get("recording_id") or args.get("id") or ""
    if not rec_id:
        return _fail("missing_param", "recording_id is required")
    recordings = await client.get_recordings()
    # Match by exact DVR ID first, then by Path basename / filename stem.
    target = None
    for r in recordings:
        if str(r.get("ID", "")) == str(rec_id):
            target = r
            break
    if target is None:
        for r in recordings:
            path = r.get("Path", "") or ""
            if path and (path.endswith(rec_id) or path.endswith(f"{rec_id}.mpg") or
                         rec_id in path):
                target = r
                break
    if target is None:
        return _fail("not_found", f"No recording with id {rec_id}")
    return _ok(data=_enrich_channels_recording(target), message="recording")


async def _list_genres(client, args: Dict, bridge=None) -> Dict:
    """List all distinct genres across Channels DVR recordings."""
    recordings = await client.get_recordings()
    genre_counts = {}
    for rec in recordings:
        airing = rec.get("Airing") or {}
        for g in (airing.get("Genres") or []):
            g = g.strip()
            if g:
                genre_counts[g] = genre_counts.get(g, 0) + 1
    sorted_genres = sorted(genre_counts.items(), key=lambda x: -x[1])
    return _ok(data={"genres": [{"name": g, "count": c} for g, c in sorted_genres]},
               message=f"{len(sorted_genres)} genres across {len(recordings)} recordings")


async def _search_recordings(client, args: Dict, bridge=None) -> Dict:
    """Search Channels DVR recordings with filters."""
    from datetime import datetime
    title = args.get("title", "")
    episode_title = args.get("episode_title", "")
    channel = args.get("channel", "")
    actor = args.get("actor", "")
    genre = args.get("genre", "")
    season = args.get("season")
    episode = args.get("episode")
    start_date = args.get("start_date")
    end_date = args.get("end_date")
    watched = args.get("watched")  # None=any, true=watched only, false=unwatched only
    limit = int(args.get("limit", 50))

    # ── Sanitize LLM-provided args ──
    # The 7B model often sends empty strings, zeros, and false as "defaults"
    # when it means "no filter".  Normalize them all to None/empty.
    if season is not None and (season == "" or int(season) == 0):
        season = None
    if episode is not None and (episode == "" or int(episode) == 0):
        episode = None
    # watched=false is sent by the LLM as a default — treat as "any"
    # Only filter on watched when explicitly True (watched-only).
    if watched is False:
        watched = None

    start_epoch = None
    end_epoch = None
    if start_date:
        try:
            start_epoch = _date_str_to_epoch(start_date, end_of_day=False)
        except ValueError:
            return _fail("invalid_date", f"Invalid start_date: {start_date}. Use YYYY-MM-DD.")
    if end_date:
        try:
            end_epoch = _date_str_to_epoch(end_date, end_of_day=True)
        except ValueError:
            return _fail("invalid_date", f"Invalid end_date: {end_date}. Use YYYY-MM-DD.")

    # When searching by date, include deleted (trashed) recordings so the
    # LLM can report what was recorded vs what's still available.
    include_deleted = bool(start_epoch or end_epoch)
    recordings = await client.get_recordings(include_deleted=include_deleted)
    results = []
    for rec in recordings:
        airing = rec.get("Airing") or {}

        if title:
            rec_title = airing.get("Title", "")
            ep_title = airing.get("EpisodeTitle", "")
            combined = f"{rec_title} {ep_title}".lower()
            if title.lower() not in combined:
                continue

        if episode_title:
            rec_ep_title = airing.get("EpisodeTitle", "")
            if episode_title.lower() not in rec_ep_title.lower():
                continue

        if channel:
            rec_ch = str(airing.get("Channel", ""))
            if channel.lower() not in rec_ch.lower():
                continue

        if actor:
            rec_cast = airing.get("Cast") or []
            cast_str = " ".join(rec_cast).lower()
            if actor.lower() not in cast_str:
                continue

        if genre:
            rec_genres = airing.get("Genres") or []
            genres_str = " ".join(rec_genres).lower()
            if genre.lower() not in genres_str:
                continue

        if season is not None:
            rec_season = airing.get("SeasonNumber", 0)
            if int(season) != int(rec_season):
                continue

        if episode is not None:
            rec_episode = airing.get("EpisodeNumber", 0)
            if int(episode) != int(rec_episode):
                continue

        rec_time = airing.get("Time") or rec.get("CreatedAt") or 0
        if start_epoch and int(rec_time) < start_epoch:
            continue
        if end_epoch and int(rec_time) > end_epoch:
            continue

        if watched is not None:
            is_watched = bool(rec.get("Watched") or rec.get("PlayedAt"))
            if bool(watched) != is_watched:
                continue

        results.append(rec)
        if len(results) >= limit:
            break

    enriched = _enrich_channels_recordings(results)
    response = {"results": enriched}

    # When searching by date, also include failed recordings from that period
    if start_epoch or end_epoch:
        jobs = await client.get_jobs()
        failed = []
        for j in jobs:
            if not (j.get("Failed") or j.get("Dead")):
                continue
            airing = j.get("Airing", {})
            start = airing.get("Time", j.get("Time", 0))
            if start_epoch and int(start) < start_epoch:
                continue
            if end_epoch and int(start) > end_epoch:
                continue
            if title:
                j_title = (airing.get("Title", "") + " " + airing.get("EpisodeTitle", "")).lower()
                if title.lower() not in j_title:
                    continue
            dt = datetime.fromtimestamp(start)
            failed.append({
                "title": airing.get("Title") or j.get("Name", ""),
                "episode_title": airing.get("EpisodeTitle", ""),
                "channel": (j.get("Channels") or [""])[0],
                "start_time": dt.strftime("%Y-%m-%d %I:%M %p"),
                "error": j.get("Error", ""),
                "status": "failed",
            })
        if failed:
            response["failed_recordings"] = failed

    n_avail = sum(1 for r in enriched if r.get("status") == "available")
    n_removed = sum(1 for r in enriched if r.get("status") == "watched_and_removed")
    total = len(enriched)
    # Build a summary message that the 7B LLM will reliably read.
    lines = []
    if n_removed:
        lines.append(f"{total} shows were recorded. {n_avail} are still on the DVR. "
                      f"{n_removed} were already watched and are no longer on the DVR")
    else:
        lines.append(f"Found {total} recording(s), all on the DVR")
    if response.get("failed_recordings"):
        lines.append(f"{len(response['failed_recordings'])} failed recordings")
    msg = ". ".join(lines)
    return _ok(data=response, message=msg)


async def _get_scheduled_recordings(client, args: Dict, bridge=None) -> Dict:
    rules = await client.get_rules()
    return _ok(data=rules, message=f"{len(rules)} recording rules")


async def _get_channels(client, args: Dict, bridge=None) -> Dict:
    channels = await client.get_channels()
    slimmed = []
    for ch in channels:
        slimmed.append({
            "number": ch.get("Number") or ch.get("GuideNumber", ""),
            "name": ch.get("Name") or ch.get("GuideName", ""),
            "network": ch.get("Station", ""),
            "hd": ch.get("HD", False),
        })
    return _ok(data=slimmed, message=f"{len(slimmed)} channels")


async def _search_epg(client, args: Dict, bridge=None) -> Dict:
    query = args.get("query", "")
    if not query:
        return _fail("missing_param", "query is required")
    results = await client.search_epg(query)
    return _ok(data=results, message=f"{len(results)} results for '{query}'")


async def _get_storage_status(client, args: Dict, bridge=None) -> Dict:
    dvr = await client.dvr_info()
    return _ok(data={
        "path": dvr.get("path", ""),
        "extra_paths": dvr.get("extra_paths", []),
        "disk": dvr.get("disk", {}),
        "stats": dvr.get("stats", {}),
    }, message="Storage status retrieved")


async def _get_jobs(client, args: Dict, bridge=None) -> Dict:
    jobs = await client.get_jobs()
    status_filter = args.get("status", "").lower()  # active, completed, failed, or empty=all
    if status_filter:
        filtered = []
        for j in jobs:
            is_failed = bool(j.get("Failed") or j.get("Dead"))
            is_complete = bool(j.get("Completed"))
            if status_filter == "active" and (is_failed or is_complete):
                continue
            elif status_filter == "failed" and not is_failed:
                continue
            elif status_filter == "completed" and not is_complete:
                continue
            filtered.append(j)
        jobs = filtered
    return _ok(data=jobs, message=f"{len(jobs)} jobs")


async def _get_upcoming_recordings(client, args: Dict, bridge=None) -> Dict:
    """List upcoming scheduled recordings categorized by status."""
    import time
    from datetime import datetime, timedelta
    jobs = await client.get_jobs()
    now = time.time()

    # Optional title/channel filters
    title_filter = args.get("title", "").lower()
    channel_filter = args.get("channel", "").lower()

    # Date filtering: support single date or start_date/end_date range
    start_date_str = args.get("start_date", "")
    end_date_str = args.get("end_date", "")
    date_str = args.get("date", "")

    if start_date_str or end_date_str:
        # Date range mode
        try:
            if start_date_str:
                range_start = datetime.strptime(start_date_str, "%Y-%m-%d")
            else:
                range_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if end_date_str:
                range_end = datetime.strptime(end_date_str, "%Y-%m-%d") + timedelta(days=1)
            else:
                range_end = range_start + timedelta(days=1)
        except ValueError:
            return _fail("bad_param", "dates must be YYYY-MM-DD")
        day_start = range_start.timestamp()
        day_end = range_end.timestamp()
        day_label = f"{range_start.strftime('%Y-%m-%d')} to {(range_end - timedelta(days=1)).strftime('%Y-%m-%d')}"
    elif date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return _fail("bad_param", f"date must be YYYY-MM-DD, got '{date_str}'")
        day_start = day.timestamp()
        day_end = (day + timedelta(days=1)).timestamp()
        day_label = day.strftime("%Y-%m-%d")
    else:
        day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = day.timestamp()
        day_end = (day + timedelta(days=1)).timestamp()
        day_label = day.strftime("%Y-%m-%d")

    def _enrich(j):
        airing = j.get("Airing", {})
        start = airing.get("Time", j.get("Time", 0))
        dt = datetime.fromtimestamp(start)
        tags = airing.get("Tags", [])
        return {
            "title": airing.get("Title") or j.get("Name", ""),
            "episode_title": airing.get("EpisodeTitle", ""),
            "season": airing.get("SeasonNumber"),
            "episode": airing.get("EpisodeNumber"),
            "channel": (j.get("Channels") or [""])[0],
            "air_date": dt.strftime("%a %b %-d"),
            "start_time": dt.strftime("%Y-%m-%d %I:%M %p"),
            "duration_min": round(airing.get("Duration", 0) / 60),
            "original_date": airing.get("OriginalDate", ""),
            "description": airing.get("FullSummary") or airing.get("Summary", ""),
            "image": airing.get("Image", ""),
            "genres": airing.get("Genres", []),
            "cast": airing.get("Cast", [])[:5],
            "content_rating": airing.get("ContentRating", ""),
            "is_new": "New" in tags,
            "is_hd": any(t.startswith("HD") for t in tags),
            "_sort": start,
        }

    scheduled = []
    skipped = []
    for j in jobs:
        if j.get("Failed") or j.get("Dead"):
            continue  # failed jobs are past events, not upcoming
        airing = j.get("Airing", {})
        start = airing.get("Time", j.get("Time", 0))
        if start < day_start or start >= day_end:
            continue
        # Title filter
        if title_filter:
            j_title = (airing.get("Title", "") + " " + airing.get("EpisodeTitle", "")).lower()
            if title_filter not in j_title:
                continue
        # Channel filter
        if channel_filter:
            j_channels = " ".join(j.get("Channels") or []).lower()
            if channel_filter not in j_channels:
                continue
        entry = _enrich(j)
        if j.get("Skipped"):
            skipped.append(entry)
        else:
            scheduled.append(entry)

    for group in (scheduled, skipped):
        group.sort(key=lambda r: r.pop("_sort"))

    result = {"scheduled": scheduled}
    if skipped:
        result["skipped"] = skipped

    total = len(scheduled) + len(skipped)
    parts = [f"{len(scheduled)} scheduled"]
    if skipped:
        parts.append(f"{len(skipped)} skipped")
    return _ok(data=result, message=f"{total} recordings on {day_label} ({', '.join(parts)})")


async def _get_clients(client, args: Dict, bridge=None) -> Dict:
    clients = await client.get_clients()
    return _ok(data=clients, message=f"{len(clients)} clients")


# ==================================================================
# Recording tool handlers
# ==================================================================

async def _schedule_recording(client, args: Dict, bridge=None) -> Dict:
    body = {
        "ProgramID": args.get("program_id"),
        "Channel": args.get("channel"),
        "StartTime": args.get("start_time"),
        "EndTime": args.get("end_time"),
    }
    result = await client.post("/dvr/rules", json_body=body)
    return _ok(data=result, message="Recording scheduled")


async def _schedule_series_recording(client, args: Dict, bridge=None) -> Dict:
    body = {
        "SeriesID": args.get("series_id"),
        "Channel": args.get("channel"),
    }
    options = args.get("options")
    if options:
        body.update(options)
    result = await client.post("/dvr/rules", json_body=body)
    return _ok(data=result, message="Series recording scheduled")


async def _cancel_scheduled_recording(client, args: Dict, bridge=None) -> Dict:
    rule_id = args.get("id")
    if not rule_id:
        return _fail("missing_param", "id is required")
    await client.delete(f"/dvr/rules/{rule_id}")
    return _ok(message=f"Recording rule {rule_id} cancelled")


async def _delete_recording(client, args: Dict, bridge=None) -> Dict:
    file_id = args.get("id")
    if not file_id:
        return _fail("missing_param", "id is required")
    await client.delete(f"/dvr/files/{file_id}")
    return _ok(message=f"Recording {file_id} deleted")


async def _delete_recording_file(client, args: Dict, bridge=None) -> Dict:
    file_id = args.get("id")
    if not file_id:
        return _fail("missing_param", "id is required")
    await client.delete(f"/dvr/files/{file_id}", params={"delete": "true"})
    return _ok(message=f"Recording file {file_id} permanently deleted")


# ==================================================================
# Commercial tool handlers
# ==================================================================

async def _regenerate_commercial_markers(client, args: Dict, bridge=None) -> Dict:
    file_id = args.get("id")
    if not file_id:
        return _fail("missing_param", "id is required")
    await client.post(f"/dvr/files/{file_id}/commercials/rebuild")
    return _ok(message=f"Commercial markers regeneration started for {file_id}")


# ==================================================================
# System tool handlers (OWNER)
# ==================================================================

async def _clear_cache(client, args: Dict, bridge=None) -> Dict:
    await client.post("/dvr/cache/clear")
    return _ok(message="Cache cleared")


async def _rebuild_index(client, args: Dict, bridge=None) -> Dict:
    await client.post("/dvr/index/rebuild")
    return _ok(message="Index rebuild started")


# ==================================================================
# Tool registry
# ==================================================================

TOOL_REGISTRY = {
    # --- Bridge Device Management ---
    "channels_get_bridge_devices": {
        "description": "List connected Channels Bridge devices available for playback control.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _get_bridge_devices,
    },

    # --- Playback Status (via bridge) ---
    "channels_get_playback_status": {
        "description": "Get current playback status from a Channels app (what's playing, paused/playing, position).",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional, uses first connected device if omitted)"}},
        },
        "safety": Safety.SAFE,
        "handler": _get_playback_status,
    },

    # --- Playback Control (via bridge to Channels App API) ---
    "channels_pause_playback": {
        "description": "Pause playback on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _pause_playback,
    },
    "channels_resume_playback": {
        "description": "Resume playback on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _resume_playback,
    },
    "channels_toggle_pause": {
        "description": "Toggle play/pause on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _toggle_pause,
    },
    "channels_stop_playback": {
        "description": "Stop playback on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _stop_playback,
    },
    "channels_seek_relative": {
        "description": "Seek forward or backward by N seconds on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "Positive = forward, negative = backward"},
                "device": {"type": "string", "description": "Device name (optional)"},
            },
            "required": ["seconds"],
        },
        "safety": Safety.SAFE,
        "handler": _seek_relative,
    },
    "channels_seek_forward": {
        "description": "Seek forward by the default amount in Channels settings.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _seek_forward,
    },
    "channels_seek_backward": {
        "description": "Seek backward by the default amount in Channels settings.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _seek_backward,
    },
    "channels_skip_commercial": {
        "description": "Skip to the next commercial marker.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _skip_commercial,
    },
    "channels_previous_commercial": {
        "description": "Jump to the previous commercial marker.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _previous_commercial,
    },
    "channels_toggle_mute": {
        "description": "Toggle mute on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _toggle_mute,
    },
    "channels_toggle_cc": {
        "description": "Toggle closed captions on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _toggle_cc,
    },
    "channels_play_channel": {
        "description": "Tune to a channel on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_number": {"type": "string", "description": "Channel number to tune to"},
                "device": {"type": "string", "description": "Device name (optional)"},
            },
            "required": ["channel_number"],
        },
        "safety": Safety.SAFE,
        "handler": _play_channel,
    },
    "channels_play_recording": {
        "description": "Play a recording on a Channels device by recording ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recording_id": {"type": "string", "description": "Recording ID from the DVR library"},
                "device": {"type": "string", "description": "Device name (optional)"},
            },
            "required": ["recording_id"],
        },
        "safety": Safety.SAFE,
        "handler": _play_recording,
    },
    "channels_channel_up": {
        "description": "Channel up on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _channel_up,
    },
    "channels_channel_down": {
        "description": "Channel down on a Channels device.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": {"type": "string", "description": "Device name (optional)"}},
        },
        "safety": Safety.SAFE,
        "handler": _channel_down,
    },

    # --- Queries (SAFE, via DVR server API) ---
    "channels_get_now_playing": {
        "description": "Get all active playback sessions from the DVR server.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _get_now_playing,
    },
    "channels_get_recordings": {
        "description": "Get DVR recordings list.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max results (default 50)"}},
        },
        "safety": Safety.SAFE,
        "handler": _get_recordings,
    },
    "channels_get_recording": {
        "description": "Get a single recording by DVR ID or filename stem (with full enriched metadata including air dates, cast, episode info).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recording_id": {"type": "string", "description": "DVR recording ID or filename stem (without extension)"},
            },
            "required": ["recording_id"],
        },
        "safety": Safety.SAFE,
        "handler": _get_recording,
    },
    "channels_list_genres": {
        "description": "List all distinct genres across Channels DVR recordings with counts.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _list_genres,
    },
    "channels_search_recordings": {
        "description": "Search PAST recordings already saved on the DVR. USE THIS for 'what recorded yesterday/last week' or 'what has been recorded'. Supports title, episode_title, actor, genre, channel, season, episode, date range, and watched filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Show name substring filter (case-insensitive). Use the SHOW NAME only."},
                "episode_title": {"type": "string", "description": "Episode title/name substring filter (case-insensitive)."},
                "actor": {"type": "string", "description": "Actor/cast member name substring filter (case-insensitive)."},
                "genre": {"type": "string", "description": "Genre substring filter (e.g. 'drama', 'comedy', 'sci-fi'). Use channels_list_genres to see valid values."},
                "channel": {"type": "string", "description": "Channel number filter"},
                "season": {"type": "integer", "description": "Season number filter (e.g. 3 for S03)"},
                "episode": {"type": "integer", "description": "Episode number filter (e.g. 14 for E14)"},
                "start_date": {"type": "string", "description": "Minimum date (YYYY-MM-DD)"},
                "end_date": {"type": "string", "description": "Maximum date (YYYY-MM-DD)"},
                "watched": {"type": "boolean", "description": "Filter by watched status: true=watched only, false=unwatched only, omit=all"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
        },
        "safety": Safety.SAFE,
        "handler": _search_recordings,
    },
    "channels_get_scheduled_recordings": {
        "description": "Get all recording rules (scheduled recordings).",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _get_scheduled_recordings,
    },
    "channels_get_channels": {
        "description": "Get all channels from all tuner devices.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _get_channels,
    },
    "channels_search_epg": {
        "description": "Search the EPG (electronic program guide).",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search term"}},
            "required": ["query"],
        },
        "safety": Safety.SAFE,
        "handler": _search_epg,
    },
    "channels_get_storage_status": {
        "description": "Get DVR storage disk usage.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _get_storage_status,
    },
    "channels_get_jobs": {
        "description": "Get DVR jobs (recording, comskip, transcode). Use status filter to narrow results.",
        "inputSchema": {"type": "object", "properties": {
            "status": {"type": "string", "description": "Filter: 'active', 'completed', or 'failed'. Omit for all."},
        }},
        "safety": Safety.SAFE,
        "handler": _get_jobs,
    },
    "channels_get_upcoming_recordings": {
        "description": "List FUTURE scheduled recordings (episodes about to record). USE THIS for 'what is recording today/tonight/this week' or 'what will record'. Do NOT use for past recordings.",
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "Single date YYYY-MM-DD. Defaults to today."},
            "start_date": {"type": "string", "description": "Range start YYYY-MM-DD (use with end_date for multi-day queries like 'this week')."},
            "end_date": {"type": "string", "description": "Range end YYYY-MM-DD (inclusive)."},
            "title": {"type": "string", "description": "Title substring filter (case-insensitive)"},
            "channel": {"type": "string", "description": "Channel filter"},
        }},
        "safety": Safety.SAFE,
        "handler": _get_upcoming_recordings,
    },
    "channels_get_clients": {
        "description": "Get connected Channels DVR clients.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _get_clients,
    },

    # --- Recording (SAFE / CONFIRM / DANGEROUS) ---
    "channels_schedule_recording": {
        "description": "Schedule a one-time recording.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "string"},
                "channel": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO 8601 start time"},
                "end_time": {"type": "string", "description": "ISO 8601 end time"},
            },
            "required": ["program_id", "channel"],
        },
        "safety": Safety.SAFE,
        "handler": _schedule_recording,
    },
    "channels_schedule_series_recording": {
        "description": "Schedule a series (season pass) recording.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "channel": {"type": "string"},
                "options": {"type": "object", "description": "Additional options (keep, new_only, etc.)"},
            },
            "required": ["series_id"],
        },
        "safety": Safety.SAFE,
        "handler": _schedule_series_recording,
    },
    "channels_cancel_scheduled_recording": {
        "description": "Cancel a scheduled recording rule.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Recording rule ID"}},
            "required": ["id"],
        },
        "safety": Safety.CONFIRM,
        "handler": _cancel_scheduled_recording,
    },
    "channels_delete_recording": {
        "description": "Delete a recording (marks for removal).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Recording file ID"}},
            "required": ["id"],
        },
        "safety": Safety.CONFIRM,
        "handler": _delete_recording,
    },
    "channels_delete_recording_file": {
        "description": "Permanently delete recording file from disk.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Recording file ID"}},
            "required": ["id"],
        },
        "safety": Safety.DANGEROUS,
        "handler": _delete_recording_file,
    },

    # --- Commercial (CONFIRM) ---
    "channels_regenerate_commercial_markers": {
        "description": "Regenerate commercial skip markers for a recording.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Recording file ID"}},
            "required": ["id"],
        },
        "safety": Safety.CONFIRM,
        "handler": _regenerate_commercial_markers,
    },

    # --- System (OWNER) ---
    "channels_clear_cache": {
        "description": "Clear the Channels DVR cache.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.OWNER,
        "handler": _clear_cache,
    },
    "channels_rebuild_index": {
        "description": "Rebuild the Channels DVR media index.",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.OWNER,
        "handler": _rebuild_index,
    },
}
