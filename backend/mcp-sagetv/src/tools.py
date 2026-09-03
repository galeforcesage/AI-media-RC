"""
tools.py
Complete MCP tool registry for SageTV.

Each tool maps to a sagex-api command per Appendix A / Appendix G.
Tools are namespaced with sagetv_ prefix.
"""

from __future__ import annotations
import datetime
import enum
import logging
import re
from typing import Any, Callable, Coroutine, Dict

logger = logging.getLogger(__name__)

# Pattern matching SageTV server names used as fallback ShowTitle for imports
_RE_SAGETV_NAME = re.compile(r'^SageTV\d*$', re.IGNORECASE)


# ------------------------------------------------------------------
# Safety levels
# ------------------------------------------------------------------

class Safety(str, enum.Enum):
    SAFE = "SAFE"
    CONFIRM = "CONFIRM"
    DANGEROUS = "DANGEROUS"
    OWNER = "OWNER"


# ------------------------------------------------------------------
# Helper to build success / error dicts
# ------------------------------------------------------------------

def _ok(data: Any = None, message: str = "OK") -> Dict[str, Any]:
    r: Dict[str, Any] = {"success": True, "message": message}
    if data is not None:
        r["data"] = data
    return r


def _fail(error: str, message: str, suggestions: list | None = None) -> Dict[str, Any]:
    r: Dict[str, Any] = {"success": False, "error": error, "message": message}
    if suggestions:
        r["suggestions"] = suggestions
    return r


def _epoch_ms_to_readable(epoch_ms: int) -> str:
    """Convert epoch milliseconds to a human-readable date string."""
    try:
        dt = datetime.datetime.fromtimestamp(epoch_ms / 1000.0)
        return dt.strftime("%Y-%m-%d %I:%M %p")
    except (ValueError, OSError, TypeError):
        return ""


def _date_str_to_epoch_ms(date_str: str, end_of_day: bool = False) -> int:
    """Convert a YYYY-MM-DD string to epoch milliseconds.
    If end_of_day=True, returns 23:59:59.999 of that day."""
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(dt.timestamp() * 1000)


def _enrich_recording(mf: Dict) -> Dict:
    """Add human-readable date fields to a recording object so the LLM can read them."""
    if not isinstance(mf, dict):
        return mf
    for field in ("FileStartTime", "FileStartTime"):
        val = mf.get(field)
        if val:
            mf["StartDate"] = _epoch_ms_to_readable(int(val))
            break
    for field in ("FileEndTime", "FileEndTime"):
        val = mf.get(field)
        if val:
            mf["EndDate"] = _epoch_ms_to_readable(int(val))
            break
    # Also enrich the airing start/end
    airing = mf.get("Airing")
    if airing and isinstance(airing, dict):
        val = airing.get("AiringStartTime")
        if val:
            airing["AiringStartDate"] = _epoch_ms_to_readable(int(val))
        val = airing.get("AiringEndTime")
        if val:
            airing["AiringEndDate"] = _epoch_ms_to_readable(int(val))
    return mf


def _slim_recording(mf: Dict) -> Dict:
    """Extract only the fields the LLM needs from a SageTV recording.

    Raw SageTV MediaFile objects are 3-5 KB each (nested Airing→Show→Channel,
    file paths, segment info, metadata IDs, etc.).  50 recordings = 150-250 KB
    which exceeds the agent's 4 KB truncation limit.  Return a compact dict
    matching the Channels enrichment shape so the LLM gets consistent data.
    """
    if not isinstance(mf, dict):
        return mf
    airing = mf.get("Airing") or {}
    show = airing.get("Show") or {}
    channel = airing.get("Channel") or {}
    start_ms = mf.get("FileStartTime") or airing.get("AiringStartTime") or 0
    season = show.get("ShowSeasonNumber")
    episode = show.get("ShowEpisodeNumber")
    se = ""
    if isinstance(season, int) and isinstance(episode, int):
        se = f"S{season:02d}E{episode:02d}"

    title = show.get("ShowTitle", "")
    ep_title = show.get("ShowEpisode", "")

    # Imported files often have ShowTitle = server name (e.g. "SageTV9") and
    # the real show name embedded in ShowEpisode as a filename like
    # "MotorWeek-S37E09-2018LexusLC500-5549956-0".  Extract the real title.
    if _RE_SAGETV_NAME.match(title) and ep_title and "-" in ep_title:
        parts = ep_title.split("-")
        title = parts[0]
        # The remainder after title-SxxExx is the episode description
        rest = "-".join(parts[1:])
        # Strip the SxxExx and trailing ID/segment numbers
        rest = re.sub(r'^S\d+E\d+-', '', rest)
        rest = re.sub(r'-\d+-\d+$', '', rest)
        if rest:
            ep_title = rest

    result = {
        "id": str(mf.get("MediaFileID", "")),
        "title": title,
        "episode_title": ep_title,
        "season_episode": se,
        "channel": channel.get("ChannelName", ""),
        "recorded": _epoch_ms_to_readable(int(start_ms)) if start_ms else "",
        "record_date": int(start_ms) // 1000 if start_ms else None,
        "duration_min": round((mf.get("FileDuration", 0) or 0) / 60000, 1),
        "description": show.get("ShowDescription", ""),
        "genres": show.get("ShowCategory", ""),
        "image": show.get("ShowImage", ""),
        "cast": show.get("PeopleListInShow", []),
        "content_rating": show.get("ShowParentalRating", ""),
        "watched": bool(airing.get("IsWatched", False)),
    }
    # Status: SageTV files are always on disk (no trash concept).
    # Distinguish between in-progress, archived (protected), and available.
    if not mf.get("IsCompleteRecording", True):
        result["status"] = "recording"
    elif mf.get("IsLibraryFile", False):
        result["status"] = "archived"
    else:
        result["status"] = "available"
    return result


def _slim_recordings(data: Any) -> Any:
    """Slim a list of SageTV recordings to LLM-friendly compact dicts."""
    if isinstance(data, list):
        return [_slim_recording(mf) for mf in data]
    return data


def _enrich_recordings(data: Any) -> Any:
    """Enrich a list of recordings with readable dates."""
    if isinstance(data, list):
        return [_enrich_recording(mf) for mf in data]
    return data


# ==================================================================
# Tool handler type
# ==================================================================
# Each handler: async (sagex_client, arguments) -> dict

from .sagex_client import SageXClient  # noqa: E402


# ==================================================================
# PLAYBACK TOOLS
# ==================================================================

async def sagetv_pause_playback(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("MediaPlayerAPI.Pause", context=ctx)
    return _ok(message="Playback paused")


async def sagetv_resume_playback(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("MediaPlayerAPI.Play", context=ctx)
    return _ok(message="Playback resumed")


async def sagetv_toggle_playback(client: SageXClient, args: Dict) -> Dict:
    """Toggle play/pause on the active SageTV session."""
    ctx = args.get("session_id", "")
    # Try to detect current state; Placeshifter may report unreliable values
    has_media = await client.call("MediaPlayerAPI.HasMediaFile", context=ctx)
    if not has_media:
        # Nothing loaded — try to play
        await client.call("MediaPlayerAPI.Play", context=ctx)
        return _ok(data={"state": "playing"}, message="Playback started")

    # For Placeshifters, IsPlaying may be unreliable (always false).
    # Use the explicit action hint from the caller if provided.
    hint = args.get("action_hint", "")
    if hint == "play":
        await client.call("MediaPlayerAPI.Play", context=ctx)
        return _ok(data={"state": "playing"}, message="Playback resumed")
    elif hint == "pause":
        await client.call("MediaPlayerAPI.Pause", context=ctx)
        return _ok(data={"state": "paused"}, message="Playback paused")

    # No hint — try IsPlaying detection
    is_playing = await client.call("MediaPlayerAPI.IsPlaying", context=ctx)
    if is_playing:
        await client.call("MediaPlayerAPI.Pause", context=ctx)
        return _ok(data={"state": "paused"}, message="Playback paused")
    else:
        await client.call("MediaPlayerAPI.Play", context=ctx)
        return _ok(data={"state": "playing"}, message="Playback resumed")


async def sagetv_stop_playback(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("MediaPlayerAPI.CloseAndWaitUntilClosed", context=ctx)
    return _ok(message="Playback stopped")


async def sagetv_skip_forward(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("MediaPlayerAPI.SkipForward", context=ctx)
    return _ok(message="Skipped forward")


async def sagetv_skip_back(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("MediaPlayerAPI.SkipBackward", context=ctx)
    return _ok(message="Skipped back")


async def sagetv_seek_relative(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    seconds = args.get("seconds", 0)
    millis = int(seconds) * 1000
    current = await client.call("MediaPlayerAPI.GetMediaTime", context=ctx)
    new_pos = max(0, int(current or 0) + millis)
    await client.call("MediaPlayerAPI.Seek", [str(new_pos)], context=ctx)
    return _ok(data={"position_ms": new_pos}, message=f"Seeked {seconds}s relative")


async def sagetv_seek_absolute(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    position_seconds = int(args.get("position_seconds", 0))
    millis = position_seconds * 1000
    await client.call("MediaPlayerAPI.Seek", [str(millis)], context=ctx)
    return _ok(data={"position_ms": millis}, message=f"Seeked to {position_seconds}s")


async def sagetv_set_volume(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    level = max(0, min(100, int(args.get("level", 50))))
    volume_float = level / 100.0
    await client.call("MediaPlayerAPI.SetVolume", [str(volume_float)], context=ctx)
    return _ok(data={"level": level}, message=f"Volume set to {level}%")


async def sagetv_mute(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("MediaPlayerAPI.SetMute", ["true"], context=ctx)
    return _ok(message="Muted")


async def sagetv_unmute(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("MediaPlayerAPI.SetMute", ["false"], context=ctx)
    return _ok(message="Unmuted")


async def sagetv_tune_channel(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    channel = str(args.get("channel", ""))
    if not channel:
        return _fail("missing_channel", "Channel number or name is required")
    await client.call("MediaPlayerAPI.ChannelSet", [channel], context=ctx)
    return _ok(data={"channel": channel}, message=f"Tuned to channel {channel}")


# ==================================================================
# QUERY TOOLS
# ==================================================================

async def sagetv_get_now_playing(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    media = await client.call("MediaPlayerAPI.GetCurrentMediaFile", context=ctx)
    if not media:
        return _ok(data=None, message="Nothing currently playing")
    return _ok(data=media, message="Current media retrieved")


async def sagetv_get_recordings(client: SageXClient, args: Dict) -> Dict:
    size = int(args.get("limit", 50))
    start = int(args.get("offset", 0))
    data = await client.call("GetMediaFiles", ["T"], start=start, size=size)
    return _ok(data=_slim_recordings(data), message="Recordings retrieved")


async def sagetv_get_upcoming_recordings(client: SageXClient, args: Dict) -> Dict:
    start_date_str = args.get("start_date", "")
    end_date_str = args.get("end_date", "")
    data = await client.call("GetScheduledRecordings")
    if not data or not isinstance(data, list):
        return _ok(data=[], message="No upcoming recordings")

    # Parse date range filter
    range_start = None
    range_end = None
    if start_date_str or end_date_str:
        from datetime import datetime as _dt, timedelta as _td
        try:
            if start_date_str:
                range_start = _dt.strptime(start_date_str, "%Y-%m-%d")
            if end_date_str:
                range_end = _dt.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        except ValueError:
            pass

    slimmed = []
    for item in data:
        airing = item if "AiringStartTime" in item else item.get("Airing", item)
        show = airing.get("Show") or {}
        channel = airing.get("Channel") or {}
        start_ms = airing.get("AiringStartTime", 0)

        # Apply date range filter
        if (range_start or range_end) and start_ms:
            from datetime import datetime as _dt2
            air_dt = _dt2.fromtimestamp(int(start_ms) / 1000)
            if range_start and air_dt < range_start:
                continue
            if range_end and air_dt > range_end:
                continue

        season = show.get("ShowSeasonNumber")
        episode = show.get("ShowEpisodeNumber")
        se = f"S{season:02d}E{episode:02d}" if isinstance(season, int) and isinstance(episode, int) else ""
        # Short air_date for display
        air_date_str = ""
        if start_ms:
            try:
                import datetime as _dtmod
                air_dt_val = _dtmod.datetime.fromtimestamp(int(start_ms) / 1000)
                air_date_str = air_dt_val.strftime("%a %b %-d")
            except Exception:
                pass
        slimmed.append({
            "title": show.get("ShowTitle", ""),
            "episode_title": show.get("ShowEpisode", ""),
            "season_episode": se,
            "channel": channel.get("ChannelName", ""),
            "air_date": air_date_str,
            "start_time": _epoch_ms_to_readable(int(start_ms)) if start_ms else "",
        })
    return _ok(data=slimmed, message=f"{len(slimmed)} upcoming recordings")


async def sagetv_get_channels(client: SageXClient, args: Dict) -> Dict:
    data = await client.call("GetAllChannels")
    if not data or not isinstance(data, list):
        return _ok(data=[], message="No channels")
    slimmed = []
    for ch in data:
        slimmed.append({
            "number": ch.get("ChannelNumber", ""),
            "name": ch.get("ChannelName", ""),
            "network": ch.get("ChannelNetwork", ""),
        })
    return _ok(data=slimmed, message=f"{len(slimmed)} channels")


async def sagetv_search_shows(client: SageXClient, args: Dict) -> Dict:
    query = str(args.get("query", ""))
    if not query:
        return _fail("missing_query", "Search query is required")
    data = await client.call("SearchSelectedFieldsRegex", [query, "Title", "false", "false", "false", "false", "false", "false", "false", "false", "true", ""])
    return _ok(data=data, message=f"Search results for '{query}'")


async def sagetv_get_disk_space(client: SageXClient, args: Dict) -> Dict:
    total = await client.call("GetTotalDiskspaceAvailable")
    used = await client.call("GetUsedVideoDiskspace")
    return _ok(data={"available_bytes": total, "used_bytes": used})


async def sagetv_get_tuner_status(client: SageXClient, args: Dict) -> Dict:
    data = await client.call("GetCaptureDevices")
    return _ok(data=data, message="Tuner status retrieved")


async def sagetv_get_clients(client: SageXClient, args: Dict) -> Dict:
    data = await client.call("GetConnectedClients")
    return _ok(data=data, message="Connected clients retrieved")


async def sagetv_get_ui_contexts(client: SageXClient, args: Dict) -> Dict:
    """Get all active SageTV UI contexts (server, placeshifters, extenders, clients)."""
    contexts = await client.call("GetUIContextNames")
    if not contexts:
        return _ok(data=[], message="No UI contexts found")
    if isinstance(contexts, str):
        contexts = [contexts]

    results = []
    for ctx_name in contexts:
        info = {"context_id": ctx_name}
        try:
            has_media = await client.call("MediaPlayerAPI.HasMediaFile", context=ctx_name)
            title = await client.call("MediaPlayerAPI.GetCurrentMediaTitle", context=ctx_name)
            info["title"] = title or ""
            info["has_media"] = bool(has_media)
            if has_media:
                loaded = await client.call("MediaPlayerAPI.IsMediaPlayerFullyLoaded", context=ctx_name)
                info["loaded"] = bool(loaded)
                try:
                    is_playing = await client.call("MediaPlayerAPI.IsPlaying", context=ctx_name)
                    info["state"] = "playing" if is_playing else "paused"
                except Exception:
                    info["state"] = "loaded" if loaded else "idle"
            else:
                info["loaded"] = False
                info["state"] = "idle"
        except Exception:
            info["title"] = ""
            info["has_media"] = False
            info["loaded"] = False
            info["state"] = "unknown"
        results.append(info)

    return _ok(data=results, message=f"{len(results)} UI contexts found")


async def sagetv_get_context_info(client: SageXClient, args: Dict) -> Dict:
    """Get detailed playback state for a specific SageTV UI context."""
    ctx = args.get("session_id", "")
    if not ctx:
        return _fail("missing_session_id", "session_id (context ID) is required")

    has_media = await client.call("MediaPlayerAPI.HasMediaFile", context=ctx)
    if not has_media:
        return _ok(data={"context_id": ctx, "state": "idle", "title": ""})

    title = await client.call("MediaPlayerAPI.GetCurrentMediaTitle", context=ctx) or ""
    loaded = await client.call("MediaPlayerAPI.IsMediaPlayerFullyLoaded", context=ctx)

    try:
        is_playing = await client.call("MediaPlayerAPI.IsPlaying", context=ctx)
        state = "playing" if is_playing else "paused"
    except Exception:
        state = "loaded" if loaded else "idle"

    position_ms = await client.call("MediaPlayerAPI.GetMediaTime", context=ctx) or 0
    duration_ms = await client.call("MediaPlayerAPI.GetMediaDuration", context=ctx) or 0

    # GetMediaTime returns epoch ms for recordings; convert to relative position
    # by checking if it looks like an epoch timestamp (> year 2000 in ms)
    pos = int(position_ms)
    dur = int(duration_ms)
    if pos > 946684800000:  # epoch ms > year 2000
        # Epoch-based position — try to compute relative from recording start
        # Duration in this case is often the buffer size, not the show length
        # Just report 0 and let the UI handle it gracefully
        pos = 0
        dur = 0

    return _ok(data={
        "context_id": ctx,
        "title": title,
        "state": state,
        "position": pos / 1000,
        "duration": dur / 1000,
        "loaded": bool(loaded),
    })


# ==================================================================
# RECORDING TOOLS
# ==================================================================

async def sagetv_record_show(client: SageXClient, args: Dict) -> Dict:
    airing_id = str(args.get("airing_id", ""))
    if not airing_id:
        return _fail("missing_airing_id", "Airing ID is required")
    airing = await client.call("GetAiringForID", [airing_id])
    if not airing:
        return _fail("invalid_airing_id", f"Airing {airing_id} not found",
                      ["Search for the show", "List upcoming airings"])
    await client.call("Record", [airing_id])
    return _ok(message=f"Recording set for airing {airing_id}")


async def sagetv_cancel_recording(client: SageXClient, args: Dict) -> Dict:
    airing_id = str(args.get("airing_id", ""))
    if not airing_id:
        return _fail("missing_airing_id", "Airing ID is required")
    await client.call("CancelRecord", [airing_id])
    return _ok(message=f"Recording cancelled for airing {airing_id}")


async def sagetv_delete_media_file(client: SageXClient, args: Dict) -> Dict:
    media_file_id = str(args.get("media_file_id", ""))
    if not media_file_id:
        return _fail("missing_media_file_id", "Media file ID is required")
    mf = await client.call("GetMediaFileForID", [media_file_id])
    if not mf:
        return _fail("invalid_media_file_id", f"Media file {media_file_id} not found")
    await client.call("DeleteFile", [media_file_id])
    return _ok(message=f"Media file {media_file_id} deleted")


# ==================================================================
# FAVORITES TOOLS
# ==================================================================

async def sagetv_create_favorite(client: SageXClient, args: Dict) -> Dict:
    title = str(args.get("title", ""))
    if not title:
        return _fail("missing_title", "Title is required to create a favorite")
    channel = args.get("channel")
    fav_args = [title]
    if channel:
        fav_args.append(str(channel))
    await client.call("AddFavorite", fav_args)
    return _ok(message=f"Favorite created for '{title}'")


async def sagetv_remove_favorite(client: SageXClient, args: Dict) -> Dict:
    favorite_id = str(args.get("favorite_id", ""))
    if not favorite_id:
        return _fail("missing_favorite_id", "Favorite ID is required")
    await client.call("RemoveFavorite", [favorite_id])
    return _ok(message=f"Favorite {favorite_id} removed")


# ==================================================================
# CONFIGURATION TOOLS
# ==================================================================

async def sagetv_get_config_value(client: SageXClient, args: Dict) -> Dict:
    key = str(args.get("key", ""))
    if not key:
        return _fail("missing_key", "Configuration key is required")
    value = await client.call("GetProperty", [key, ""])
    return _ok(data={"key": key, "value": value})


async def sagetv_set_config_value(client: SageXClient, args: Dict) -> Dict:
    key = str(args.get("key", ""))
    value = str(args.get("value", ""))
    if not key:
        return _fail("missing_key", "Configuration key is required")
    await client.call("SetProperty", [key, value])
    return _ok(message=f"Configuration '{key}' set to '{value}'")


# ==================================================================
# SYSTEM TOOLS
# ==================================================================

async def sagetv_run_library_scan(client: SageXClient, args: Dict) -> Dict:
    await client.call("RunLibraryImportScan", ["true"])
    return _ok(message="Library import scan started")


# ==================================================================
# NAVIGATION TOOLS
# ==================================================================

async def sagetv_open_recordings(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Recordings"], context=ctx)
    return _ok(message="Opened recordings screen")


async def sagetv_open_guide(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Program Guide"], context=ctx)
    return _ok(message="Opened program guide")


async def sagetv_open_home(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Home"], context=ctx)
    return _ok(message="Opened home screen")


async def sagetv_open_live_tv(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Live TV"], context=ctx)
    return _ok(message="Opened live TV")


async def sagetv_channel_up(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Channel Up"], context=ctx)
    return _ok(message="Channel up")


async def sagetv_channel_down(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Channel Down"], context=ctx)
    return _ok(message="Channel down")


async def sagetv_nav_up(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Up"], context=ctx)
    return _ok(message="Navigate up")


async def sagetv_nav_down(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Down"], context=ctx)
    return _ok(message="Navigate down")


async def sagetv_nav_left(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Left"], context=ctx)
    return _ok(message="Navigate left")


async def sagetv_nav_right(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Right"], context=ctx)
    return _ok(message="Navigate right")


async def sagetv_nav_select(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Select"], context=ctx)
    return _ok(message="Select")


async def sagetv_nav_back(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Back"], context=ctx)
    return _ok(message="Back")


async def sagetv_nav_options(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Options"], context=ctx)
    return _ok(message="Options menu")


async def sagetv_page_up(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Page Up"], context=ctx)
    return _ok(message="Page up")


async def sagetv_page_down(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Page Down"], context=ctx)
    return _ok(message="Page down")


async def sagetv_toggle_cc(client: SageXClient, args: Dict) -> Dict:
    """Toggle closed captions / subtitles."""
    ctx = args.get("session_id", "")
    # SageTV uses the Options > Subtitles flow, but SageCommand "CC" toggles directly
    await client.call("SageCommand", ["CC"], context=ctx)
    return _ok(message="Closed captions toggled")


async def sagetv_close(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Close"], context=ctx)
    return _ok(message="Close/exit sent")


async def sagetv_power_off(client: SageXClient, args: Dict) -> Dict:
    ctx = args.get("session_id", "")
    await client.call("SageCommand", ["Power Off"], context=ctx)
    return _ok(message="Power off sent")


# ==================================================================
# COMMERCIAL SKIP TOOLS
# ==================================================================

async def sagetv_commercial_skip(client: SageXClient, args: Dict) -> Dict:
    """Skip to the end of the current commercial break using Comskip markers."""
    ctx = args.get("session_id", "")

    # Get what's currently playing
    media = await client.call("MediaPlayerAPI.GetCurrentMediaFile", context=ctx)
    if not media:
        return _fail("nothing_playing", "Nothing is currently playing")

    # Get current position in ms
    current_ms = await client.call("MediaPlayerAPI.GetMediaTime", context=ctx)
    current_ms = int(current_ms or 0)

    # Try to get the Airing to access commercial data
    airing_id = None
    if isinstance(media, dict):
        airing = media.get("Airing") or media.get("airing") or {}
        airing_id = str(airing.get("AiringID") or airing.get("airingID") or
                        media.get("AiringID") or media.get("airingID") or "")

    if not airing_id:
        # Fallback: just skip forward 30s
        new_pos = current_ms + 30000
        await client.call("MediaPlayerAPI.Seek", [str(new_pos)], context=ctx)
        return _ok(data={"position_ms": new_pos, "method": "skip_30s"},
                   message="No commercial data available — skipped 30s")

    # Get commercial break segments from the Comskip plugin
    # SageTV stores these as segment data on the Airing
    # Format varies: try RealStartTime-based segments first
    try:
        segments = await client.call("GetMediaFileMetadata", [
            str(media.get("MediaFileID") or media.get("mediaFileID") or ""),
            "commercial_segments"
        ])
    except Exception:
        segments = None

    if segments and isinstance(segments, str) and segments.strip():
        # Parse commercial segments: format is "start1-end1;start2-end2;..." in seconds
        try:
            breaks = []
            for seg in segments.split(";"):
                seg = seg.strip()
                if "-" in seg:
                    parts = seg.split("-", 1)
                    start_s = float(parts[0]) * 1000  # convert to ms
                    end_s = float(parts[1]) * 1000
                    breaks.append((int(start_s), int(end_s)))

            # Find the commercial break we're currently in (or the next one)
            for start_ms, end_ms in breaks:
                if start_ms <= current_ms <= end_ms:
                    # We're in a commercial — seek to end of it
                    await client.call("MediaPlayerAPI.Seek", [str(end_ms)], context=ctx)
                    skipped = (end_ms - current_ms) / 1000
                    return _ok(data={"position_ms": end_ms, "skipped_seconds": skipped,
                                     "method": "comskip_segment"},
                               message=f"Skipped commercial ({skipped:.0f}s)")

            # Not in a commercial — skip to end of next upcoming commercial
            for start_ms, end_ms in breaks:
                if start_ms > current_ms:
                    await client.call("MediaPlayerAPI.Seek", [str(end_ms)], context=ctx)
                    skipped = (end_ms - current_ms) / 1000
                    return _ok(data={"position_ms": end_ms, "skipped_seconds": skipped,
                                     "method": "comskip_next"},
                               message=f"Jumped past next commercial ({skipped:.0f}s)")

            return _ok(data={"position_ms": current_ms, "method": "no_more_commercials"},
                       message="No more commercials in this recording")
        except (ValueError, IndexError):
            pass

    # Fallback: try SageTV's built-in chapter skip (works when STV supports it)
    try:
        await client.call("SageCommand", ["NextChapter"], context=ctx)
        new_pos = await client.call("MediaPlayerAPI.GetMediaTime", context=ctx)
        new_pos = int(new_pos or 0)
        if new_pos > current_ms:
            skipped = (new_pos - current_ms) / 1000
            return _ok(data={"position_ms": new_pos, "skipped_seconds": skipped,
                             "method": "chapter_skip"},
                       message=f"Skipped to next chapter ({skipped:.0f}s)")
    except Exception:
        pass

    # Last resort: skip forward 30s
    new_pos = current_ms + 30000
    await client.call("MediaPlayerAPI.Seek", [str(new_pos)], context=ctx)
    return _ok(data={"position_ms": new_pos, "method": "skip_30s"},
               message="No commercial data — skipped 30s")


async def sagetv_get_commercial_segments(client: SageXClient, args: Dict) -> Dict:
    """Get commercial break segments for a recording (from Comskip plugin)."""
    media_file_id = str(args.get("media_file_id", ""))
    if not media_file_id:
        return _fail("missing_media_file_id", "MediaFile ID is required")

    segments_str = await client.call("GetMediaFileMetadata", [media_file_id, "commercial_segments"])
    comskip_done = await client.call("GetMediaFileMetadata", [media_file_id, "comskip_done"])

    segments = []
    if segments_str and isinstance(segments_str, str) and segments_str.strip():
        try:
            for seg in segments_str.split(";"):
                seg = seg.strip()
                if "-" in seg:
                    parts = seg.split("-", 1)
                    segments.append({
                        "start": float(parts[0]),
                        "end": float(parts[1]),
                    })
        except (ValueError, IndexError):
            pass

    return _ok(data={
        "media_file_id": media_file_id,
        "comskip_done": comskip_done in ("true", "True", "1", True),
        "segments": segments,
        "segment_count": len(segments),
    }, message=f"{len(segments)} commercial segments found")


# ==================================================================
# ENTITY LOOKUP TOOLS
# ==================================================================

async def sagetv_get_recording(client: SageXClient, args: Dict) -> Dict:
    """Get a single recording by MediaFile ID — fully hydrated with Airing + Show."""
    media_file_id = str(args.get("media_file_id", ""))
    if not media_file_id:
        return _fail("missing_media_file_id", "MediaFile ID is required")
    mf = await client.call("GetMediaFileForID", [media_file_id])
    if not mf:
        return _fail("not_found", f"MediaFile {media_file_id} not found")
    return _ok(data=mf, message="Recording retrieved")


async def sagetv_get_airing(client: SageXClient, args: Dict) -> Dict:
    """Get an airing by ID — includes embedded Show and Channel data."""
    airing_id = str(args.get("airing_id", ""))
    if not airing_id:
        return _fail("missing_airing_id", "Airing ID is required")
    airing = await client.call("GetAiringForID", [airing_id])
    if not airing:
        return _fail("not_found", f"Airing {airing_id} not found")
    return _ok(data=airing, message="Airing retrieved")


async def sagetv_get_show(client: SageXClient, args: Dict) -> Dict:
    """Get show/program metadata by external ID (e.g. EP01234567)."""
    show_id = str(args.get("show_id", ""))
    if not show_id:
        return _fail("missing_show_id", "Show external ID is required")
    show = await client.call("GetShowForExternalID", [show_id])
    if not show:
        return _fail("not_found", f"Show {show_id} not found")
    return _ok(data=show, message="Show retrieved")


async def sagetv_get_channel(client: SageXClient, args: Dict) -> Dict:
    """Get channel info by station ID."""
    station_id = str(args.get("station_id", ""))
    if not station_id:
        return _fail("missing_station_id", "Station ID is required")
    channel = await client.call("GetChannelForStationID", [station_id])
    if not channel:
        return _fail("not_found", f"Channel for station {station_id} not found")
    return _ok(data=channel, message="Channel retrieved")


# ==================================================================
# RECORDING QUERY TOOLS
# ==================================================================

async def sagetv_list_genres(client: SageXClient, args: Dict) -> Dict:
    """List all distinct genres/categories across SageTV recordings."""
    data = await client.call("GetMediaFiles", ["T"])
    if not data or not isinstance(data, list):
        return _ok(data={"genres": []}, message="No recordings found")
    genre_counts = {}
    for mf in data:
        show = (mf.get("Airing") or {}).get("Show") or {}
        cat = show.get("ShowCategory", "")
        if cat:
            for g in cat.split("/"):
                g = g.strip()
                if g:
                    genre_counts[g] = genre_counts.get(g, 0) + 1
    sorted_genres = sorted(genre_counts.items(), key=lambda x: -x[1])
    return _ok(data={"genres": [{"name": g, "count": c} for g, c in sorted_genres]},
               message=f"{len(sorted_genres)} genres across {len(data)} recordings")


async def sagetv_search_recordings(client: SageXClient, args: Dict) -> Dict:
    """Search recordings with multiple filter criteria."""
    title = args.get("title", "")
    episode_title = args.get("episode_title", "")
    channel = args.get("channel", "")
    actor = args.get("actor", "")
    genre = args.get("genre", "")
    season = args.get("season")
    episode = args.get("episode")
    start_time = args.get("start_time")
    end_time = args.get("end_time")
    # Accept human-readable date strings (YYYY-MM-DD) and convert to epoch ms
    start_date = args.get("start_date")
    end_date = args.get("end_date")
    if start_date and not start_time:
        try:
            start_time = _date_str_to_epoch_ms(start_date, end_of_day=False)
        except ValueError:
            return _fail("invalid_date", f"Invalid start_date format: {start_date}. Use YYYY-MM-DD.")
    if end_date and not end_time:
        try:
            end_time = _date_str_to_epoch_ms(end_date, end_of_day=True)
        except ValueError:
            return _fail("invalid_date", f"Invalid end_date format: {end_date}. Use YYYY-MM-DD.")
    watched = args.get("watched")
    archived = args.get("archived")
    recording_state = args.get("recording_state")
    limit = int(args.get("limit", 50))

    # ── Sanitize LLM-provided args ──
    # The 7B model often sends empty strings, zeros, and false as "defaults"
    # when it means "no filter".  Normalize them all to None/empty.
    if season is not None and (season == "" or int(season) == 0):
        season = None
    if episode is not None and (episode == "" or int(episode) == 0):
        episode = None
    # watched=false is sent by the LLM as a default — treat as "any"
    if watched is False:
        watched = None

    data = await client.call("GetMediaFiles", ["T"], size=100000)
    if not data or not isinstance(data, list):
        return _ok(data=[], message="No recordings found")

    results = []
    for mf in data:
        airing = mf.get("Airing") or {}
        show = airing.get("Show") or {}

        if title:
            mf_title = show.get("ShowTitle", "")
            ep_title = show.get("ShowEpisode", "")
            combined = f"{mf_title} {ep_title}".lower()
            if title.lower() not in combined:
                continue

        if episode_title:
            ep_title = show.get("ShowEpisode", "")
            if episode_title.lower() not in ep_title.lower():
                continue

        if channel:
            ch = airing.get("Channel") or {}
            ch_num = str(ch.get("ChannelNumber", ""))
            ch_name = str(ch.get("ChannelName", ""))
            if channel.lower() not in ch_num.lower() and channel.lower() not in ch_name.lower():
                continue

        if actor:
            # SageX uses PeopleInShow (comma-separated string)
            people_str = show.get("PeopleInShow", "")
            if not isinstance(people_str, str):
                people_str = " ".join(str(p) for p in people_str)
            if actor.lower() not in people_str.lower():
                continue

        if genre:
            rec_cat = show.get("ShowCategory", "")
            if genre.lower() not in rec_cat.lower():
                continue

        if season is not None:
            rec_season = show.get("ShowSeasonNumber", 0)
            if int(season) != int(rec_season):
                continue

        if episode is not None:
            rec_episode = show.get("ShowEpisodeNumber", 0)
            if int(episode) != int(rec_episode):
                continue

        mf_start = mf.get("FileStartTime") or airing.get("AiringStartTime") or 0
        mf_end = mf.get("FileEndTime") or airing.get("AiringEndTime") or 0
        if start_time and int(mf_start) < int(start_time):
            continue
        if end_time and int(mf_end) > int(end_time):
            continue

        if watched is not None:
            is_watched = airing.get("IsWatched", False)
            if bool(watched) != bool(is_watched):
                continue

        if archived is not None:
            is_lib = mf.get("IsLibraryFile", False)
            if bool(archived) != bool(is_lib):
                continue

        if recording_state is not None:
            is_complete = mf.get("IsCompleteRecording", False)
            currently_recording = not bool(is_complete)
            if recording_state == "recording" and not currently_recording:
                continue
            if recording_state == "complete" and currently_recording:
                continue

        results.append(mf)

    # Full library is import-ordered (oldest first); sort matches by real
    # recording start time so the newest recordings surface, then cap.
    def _raw_start(mf: Dict) -> int:
        air = mf.get("Airing") or {}
        try:
            return int(mf.get("FileStartTime") or air.get("AiringStartTime") or 0)
        except (TypeError, ValueError):
            return 0

    results.sort(key=_raw_start, reverse=True)
    results = results[:limit]

    slimmed = _slim_recordings(results)
    n_avail = sum(1 for r in slimmed if r.get("status") == "available")
    n_arch = sum(1 for r in slimmed if r.get("status") == "archived")
    n_rec = sum(1 for r in slimmed if r.get("status") == "recording")
    parts = [f"{n_avail} available"]
    if n_arch:
        parts.append(f"{n_arch} archived")
    if n_rec:
        parts.append(f"{n_rec} currently recording")
    msg = f"Found {len(results)} recording(s): " + ", ".join(parts)
    return _ok(data=slimmed, message=msg)


async def sagetv_get_recent_recordings(client: SageXClient, args: Dict) -> Dict:
    """Get the most recently completed recordings.

    SageTV's GetMediaFiles returns files in ASCENDING import order, so a bounded
    fetch (size=N) returns the OLDEST N files, not the newest. To answer "what
    recorded recently" we must pull the full library and sort by the real
    recording start time (FileStartTime). A `days` window then filters to the
    last N days by that real time so the result is truthful.
    """
    days = args.get("days")
    try:
        days = int(days) if days not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        days = None
    if days is not None and days <= 0:
        days = None
    limit = int(args.get("limit", 50 if days else 20))
    # Pull the whole library (size caps at the real total) so the newest
    # recordings — which sit at the tail of the import-ordered list — are seen.
    data = await client.call("GetMediaFiles", ["T"], size=100000)
    items = data if isinstance(data, list) else []
    if not items:
        return _ok(data=[], message="No recent recordings")

    def _raw_start(mf: Dict) -> int:
        air = mf.get("Airing") or {}
        try:
            return int(mf.get("FileStartTime") or air.get("AiringStartTime") or 0)
        except (TypeError, ValueError):
            return 0

    items.sort(key=_raw_start, reverse=True)
    if days is not None:
        import time as _time
        cutoff_ms = (int(_time.time()) - days * 86400) * 1000
        items = [mf for mf in items if _raw_start(mf) >= cutoff_ms]
    items = items[:limit]
    slim = _slim_recordings(items)
    suffix = f" in the last {days} days" if days is not None else ""
    return _ok(data=slim, message=f"{len(slim)} recent recordings{suffix}")


async def sagetv_get_active_recordings(client: SageXClient, args: Dict) -> Dict:
    """Get recordings currently in progress."""
    data = await client.call("GetCurrentlyRecordingMediaFiles")
    if not data:
        return _ok(data=[], message="No active recordings")
    items = data if isinstance(data, list) else []
    return _ok(data=items, message=f"{len(items)} active recordings")


# ==================================================================
# MUTATION TOOLS
# ==================================================================

async def sagetv_set_watched(client: SageXClient, args: Dict) -> Dict:
    """Mark a recording as watched or unwatched by airing ID."""
    airing_id = str(args.get("airing_id", ""))
    watched = args.get("watched", True)
    if not airing_id:
        return _fail("missing_airing_id", "Airing ID is required")
    if watched:
        await client.call("SetWatched", [airing_id])
        return _ok(message=f"Airing {airing_id} marked as watched")
    else:
        await client.call("ClearWatched", [airing_id])
        return _ok(message=f"Airing {airing_id} marked as unwatched")


async def sagetv_set_archived(client: SageXClient, args: Dict) -> Dict:
    """Archive (protect from auto-delete) or unarchive a recording."""
    media_file_id = str(args.get("media_file_id", ""))
    archived = args.get("archived", True)
    if not media_file_id:
        return _fail("missing_media_file_id", "MediaFile ID is required")
    if archived:
        await client.call("MoveFileToLibrary", [media_file_id])
        return _ok(message=f"MediaFile {media_file_id} archived")
    else:
        await client.call("MoveTVFileOutOfLibrary", [media_file_id])
        return _ok(message=f"MediaFile {media_file_id} unarchived")


async def sagetv_set_media_file_property(client: SageXClient, args: Dict) -> Dict:
    """Set a custom property on a MediaFile (e.g. transcript_path, embedding_id)."""
    media_file_id = str(args.get("media_file_id", ""))
    key = str(args.get("key", ""))
    value = str(args.get("value", ""))
    if not media_file_id:
        return _fail("missing_media_file_id", "MediaFile ID is required")
    if not key:
        return _fail("missing_key", "Property key is required")
    await client.call("SetMediaFileMetadata", [media_file_id, key, value])
    return _ok(data={"media_file_id": media_file_id, "key": key, "value": value},
               message=f"Property '{key}' set on MediaFile {media_file_id}")


async def sagetv_get_media_file_property(client: SageXClient, args: Dict) -> Dict:
    """Get a custom property value from a MediaFile."""
    media_file_id = str(args.get("media_file_id", ""))
    key = str(args.get("key", ""))
    if not media_file_id:
        return _fail("missing_media_file_id", "MediaFile ID is required")
    if not key:
        return _fail("missing_key", "Property key is required")
    value = await client.call("GetMediaFileMetadata", [media_file_id, key])
    return _ok(data={"media_file_id": media_file_id, "key": key, "value": value})


# Placeholder handler for event tools (actual logic in server.py transport layer)
async def _event_stub(client: SageXClient, args: Dict) -> Dict:
    return _fail("server_handled", "This tool is handled at the server transport level")


# ==================================================================
# TOOL REGISTRY
# ==================================================================

def _session_id_schema() -> Dict:
    return {"type": "object", "properties": {"session_id": {"type": "string", "description": "SageTV client/session context ID"}}, "required": []}


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ---- Playback ----
    "sagetv_pause_playback": {
        "description": "Pause playback on the active SageTV session.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_pause_playback,
    },
    "sagetv_resume_playback": {
        "description": "Resume playback on the active SageTV session.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_resume_playback,
    },
    "sagetv_stop_playback": {
        "description": "Stop playback and close the media player.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_stop_playback,
    },
    "sagetv_skip_forward": {
        "description": "Skip forward in the current media.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_skip_forward,
    },
    "sagetv_skip_back": {
        "description": "Skip back in the current media.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_skip_back,
    },
    "sagetv_seek_relative": {
        "description": "Seek forward or backward by a number of seconds.",
        "input_schema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "seconds": {"type": "integer", "description": "Seconds to seek (positive=forward, negative=backward)"},
        }, "required": ["seconds"]},
        "safety": Safety.SAFE,
        "handler": sagetv_seek_relative,
    },
    "sagetv_seek_absolute": {
        "description": "Seek to an absolute position in seconds.",
        "input_schema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "position_seconds": {"type": "integer", "description": "Position in seconds from start"},
        }, "required": ["position_seconds"]},
        "safety": Safety.SAFE,
        "handler": sagetv_seek_absolute,
    },
    "sagetv_set_volume": {
        "description": "Set the playback volume (0-100).",
        "input_schema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "level": {"type": "integer", "minimum": 0, "maximum": 100},
        }, "required": ["level"]},
        "safety": Safety.SAFE,
        "handler": sagetv_set_volume,
    },
    "sagetv_mute": {
        "description": "Mute audio.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_mute,
    },
    "sagetv_unmute": {
        "description": "Unmute audio.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_unmute,
    },
    "sagetv_tune_channel": {
        "description": "Tune to a specific TV channel for live viewing.",
        "input_schema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "channel": {"type": "string", "description": "Channel number or name"},
        }, "required": ["channel"]},
        "safety": Safety.SAFE,
        "handler": sagetv_tune_channel,
    },

    # ---- Queries ----
    "sagetv_get_now_playing": {
        "description": "Get information about what is currently playing.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_get_now_playing,
    },
    "sagetv_get_recordings": {
        "description": "List TV recordings with optional paging.",
        "input_schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Max results (default 50)"},
            "offset": {"type": "integer", "description": "Start index (default 0)"},
        }},
        "safety": Safety.SAFE,
        "handler": sagetv_get_recordings,
    },
    "sagetv_get_upcoming_recordings": {
        "description": "List upcoming scheduled recordings, optionally filtered by date range.",
        "input_schema": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "Range start YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "Range end YYYY-MM-DD"},
        }},
        "safety": Safety.SAFE,
        "handler": sagetv_get_upcoming_recordings,
    },
    "sagetv_get_channels": {
        "description": "List all available TV channels.",
        "input_schema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": sagetv_get_channels,
    },
    "sagetv_search_shows": {
        "description": "Search the EPG for shows by title.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search term"},
        }, "required": ["query"]},
        "safety": Safety.SAFE,
        "handler": sagetv_search_shows,
    },
    "sagetv_get_disk_space": {
        "description": "Get available and used disk space.",
        "input_schema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": sagetv_get_disk_space,
    },
    "sagetv_get_tuner_status": {
        "description": "Get status of all capture devices / tuners.",
        "input_schema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": sagetv_get_tuner_status,
    },
    "sagetv_get_clients": {
        "description": "List currently connected SageTV clients.",
        "input_schema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": sagetv_get_clients,
    },
    "sagetv_toggle_playback": {
        "description": "Toggle play/pause on the active SageTV session. Checks current playback rate and plays if paused, pauses if playing.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_toggle_playback,
    },
    "sagetv_get_ui_contexts": {
        "description": "Get all active SageTV UI contexts (server, placeshifters, extenders, mini-clients). Returns context_id, title, playback state for each.",
        "input_schema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": sagetv_get_ui_contexts,
    },
    "sagetv_get_context_info": {
        "description": "Get detailed playback state for a specific SageTV UI context including title, position, duration, and play/pause state.",
        "input_schema": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "SageTV UI context ID"},
        }, "required": ["session_id"]},
        "safety": Safety.SAFE,
        "handler": sagetv_get_context_info,
    },

    # ---- Recording ----
    "sagetv_record_show": {
        "description": "Set a show to record by airing ID.",
        "input_schema": {"type": "object", "properties": {
            "airing_id": {"type": "string", "description": "The airing ID to record"},
        }, "required": ["airing_id"]},
        "safety": Safety.SAFE,
        "handler": sagetv_record_show,
    },
    "sagetv_cancel_recording": {
        "description": "Cancel a scheduled recording. Requires user confirmation.",
        "input_schema": {"type": "object", "properties": {
            "airing_id": {"type": "string", "description": "The airing ID to cancel"},
        }, "required": ["airing_id"]},
        "safety": Safety.CONFIRM,
        "handler": sagetv_cancel_recording,
    },
    "sagetv_delete_media_file": {
        "description": "Permanently delete a recorded media file. This is destructive and irreversible.",
        "input_schema": {"type": "object", "properties": {
            "media_file_id": {"type": "string", "description": "The media file ID to delete"},
        }, "required": ["media_file_id"]},
        "safety": Safety.DANGEROUS,
        "handler": sagetv_delete_media_file,
    },

    # ---- Favorites ----
    "sagetv_create_favorite": {
        "description": "Create a series recording favorite.",
        "input_schema": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Show title"},
            "channel": {"type": "string", "description": "Optional channel restriction"},
        }, "required": ["title"]},
        "safety": Safety.SAFE,
        "handler": sagetv_create_favorite,
    },
    "sagetv_remove_favorite": {
        "description": "Remove a series recording favorite. Requires confirmation.",
        "input_schema": {"type": "object", "properties": {
            "favorite_id": {"type": "string", "description": "Favorite ID to remove"},
        }, "required": ["favorite_id"]},
        "safety": Safety.CONFIRM,
        "handler": sagetv_remove_favorite,
    },

    # ---- Configuration ----
    "sagetv_get_config_value": {
        "description": "Get a SageTV configuration property value.",
        "input_schema": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Property key"},
        }, "required": ["key"]},
        "safety": Safety.SAFE,
        "handler": sagetv_get_config_value,
    },
    "sagetv_set_config_value": {
        "description": "Set a SageTV configuration property. Requires confirmation.",
        "input_schema": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Property key"},
            "value": {"type": "string", "description": "Property value"},
        }, "required": ["key", "value"]},
        "safety": Safety.CONFIRM,
        "handler": sagetv_set_config_value,
    },

    # ---- System ----
    "sagetv_run_library_scan": {
        "description": "Trigger a library import scan. Requires owner authentication.",
        "input_schema": {"type": "object", "properties": {}},
        "safety": Safety.OWNER,
        "handler": sagetv_run_library_scan,
    },

    # ---- Navigation ----
    "sagetv_open_recordings": {
        "description": "Navigate to the recordings screen on a SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_open_recordings,
    },
    "sagetv_open_guide": {
        "description": "Navigate to the program guide on a SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_open_guide,
    },
    "sagetv_open_home": {
        "description": "Navigate to the home screen on a SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_open_home,
    },
    "sagetv_open_live_tv": {
        "description": "Navigate to live TV on a SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_open_live_tv,
    },
    "sagetv_channel_up": {
        "description": "Channel up on SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_channel_up,
    },
    "sagetv_channel_down": {
        "description": "Channel down on SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_channel_down,
    },
    "sagetv_nav_up": {
        "description": "Navigate up in SageTV UI menus.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_nav_up,
    },
    "sagetv_nav_down": {
        "description": "Navigate down in SageTV UI menus.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_nav_down,
    },
    "sagetv_nav_left": {
        "description": "Navigate left in SageTV UI menus.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_nav_left,
    },
    "sagetv_nav_right": {
        "description": "Navigate right in SageTV UI menus.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_nav_right,
    },
    "sagetv_nav_select": {
        "description": "Select/confirm in SageTV UI menus (like OK/Enter).",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_nav_select,
    },
    "sagetv_nav_back": {
        "description": "Go back in SageTV UI menus.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_nav_back,
    },
    "sagetv_nav_options": {
        "description": "Open options/context menu on SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_nav_options,
    },
    "sagetv_page_up": {
        "description": "Page up in SageTV UI lists.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_page_up,
    },
    "sagetv_page_down": {
        "description": "Page down in SageTV UI lists.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_page_down,
    },
    "sagetv_toggle_cc": {
        "description": "Toggle closed captions / subtitles on SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_toggle_cc,
    },
    "sagetv_close": {
        "description": "Close/exit the current SageTV screen or stop playback and return to the previous view.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_close,
    },
    "sagetv_power_off": {
        "description": "Send power off command to SageTV client.",
        "input_schema": _session_id_schema(),
        "safety": Safety.CONFIRM,
        "handler": sagetv_power_off,
    },

    # ---- Commercial Skip ----
    "sagetv_commercial_skip": {
        "description": "Skip the current commercial break using Comskip data. Reads commercial segment markers, finds the current or next break, and seeks past it. Falls back to chapter skip or 30s skip if no Comskip data is available.",
        "input_schema": _session_id_schema(),
        "safety": Safety.SAFE,
        "handler": sagetv_commercial_skip,
    },
    "sagetv_get_commercial_segments": {
        "description": "Get the commercial break segments for a recording (from Comskip plugin). Returns start/end times of each commercial break.",
        "input_schema": {"type": "object", "properties": {
            "media_file_id": {"type": "string", "description": "The MediaFile ID"},
        }, "required": ["media_file_id"]},
        "safety": Safety.SAFE,
        "handler": sagetv_get_commercial_segments,
    },

    # ---- Entity Lookup ----
    "sagetv_get_recording": {
        "description": "Get a single recording by MediaFile ID, fully hydrated with Airing + Show + Channel data. Returns: mediaFileId, filePath, fileSize, startTime, endTime, duration, isRecording, isComplete, isWatched, isArchived, recordingQuality, container, resolution, airingId, showId, channelId, and user properties.",
        "input_schema": {"type": "object", "properties": {
            "media_file_id": {"type": "string", "description": "The MediaFile ID"},
        }, "required": ["media_file_id"]},
        "safety": Safety.SAFE,
        "handler": sagetv_get_recording,
    },
    "sagetv_get_airing": {
        "description": "Get an airing (broadcast instance) by ID with embedded Show and Channel data.",
        "input_schema": {"type": "object", "properties": {
            "airing_id": {"type": "string", "description": "The Airing ID"},
        }, "required": ["airing_id"]},
        "safety": Safety.SAFE,
        "handler": sagetv_get_airing,
    },
    "sagetv_get_show": {
        "description": "Get show/program metadata by external ID (e.g. EP01234567 from Schedules Direct).",
        "input_schema": {"type": "object", "properties": {
            "show_id": {"type": "string", "description": "The show external ID"},
        }, "required": ["show_id"]},
        "safety": Safety.SAFE,
        "handler": sagetv_get_show,
    },
    "sagetv_get_channel": {
        "description": "Get channel/station info by station ID.",
        "input_schema": {"type": "object", "properties": {
            "station_id": {"type": "string", "description": "The station ID"},
        }, "required": ["station_id"]},
        "safety": Safety.SAFE,
        "handler": sagetv_get_channel,
    },

    # ---- Recording Queries ----
    "sagetv_list_genres": {
        "description": "List all distinct genres/categories across SageTV recordings with counts.",
        "input_schema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": sagetv_list_genres,
    },
    "sagetv_search_recordings": {
        "description": "Search recordings with filters: title, episode_title, actor, genre, channel, season, episode, date range, watched, archived, recording state.",
        "input_schema": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Show name substring filter (case-insensitive). Use the SHOW NAME only."},
            "episode_title": {"type": "string", "description": "Episode title/name substring filter (case-insensitive)."},
            "actor": {"type": "string", "description": "Actor/cast member name substring filter (case-insensitive)."},
            "genre": {"type": "string", "description": "Genre/category substring filter (e.g. 'drama', 'comedy'). Use sagetv_list_genres to see valid values."},
            "channel": {"type": "string", "description": "Channel number or name filter"},
            "season": {"type": "integer", "description": "Season number filter (e.g. 3 for S03)"},
            "episode": {"type": "integer", "description": "Episode number filter (e.g. 14 for E14)"},
            "start_date": {"type": "string", "description": "Minimum date (YYYY-MM-DD). Preferred over start_time."},
            "end_date": {"type": "string", "description": "Maximum date (YYYY-MM-DD). Preferred over end_time."},
            "start_time": {"type": "integer", "description": "Minimum start time (epoch ms). Use start_date instead."},
            "end_time": {"type": "integer", "description": "Maximum end time (epoch ms). Use end_date instead."},
            "watched": {"type": "boolean", "description": "Filter by watched status"},
            "archived": {"type": "boolean", "description": "Filter by archived/library status"},
            "recording_state": {"type": "string", "enum": ["recording", "complete"], "description": "Filter by recording state"},
            "limit": {"type": "integer", "description": "Max results (default 50)"},
        }},
        "safety": Safety.SAFE,
        "handler": sagetv_search_recordings,
    },
    "sagetv_get_recent_recordings": {
        "description": "Get the most recently completed recordings. Pass 'days' to restrict to recordings whose air/record date is within the last N days (filtered by real recording time, not import order).",
        "input_schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Max results (default 20, or 50 when days is set)"},
            "days": {"type": "integer", "description": "Only include recordings from the last N days (by actual record date)"},
        }},
        "safety": Safety.SAFE,
        "handler": sagetv_get_recent_recordings,
    },
    "sagetv_get_active_recordings": {
        "description": "Get recordings currently in progress (actively being recorded now).",
        "input_schema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": sagetv_get_active_recordings,
    },

    # ---- Mutations ----
    "sagetv_set_watched": {
        "description": "Mark a recording as watched or unwatched.",
        "input_schema": {"type": "object", "properties": {
            "airing_id": {"type": "string", "description": "The Airing ID"},
            "watched": {"type": "boolean", "description": "True=watched, false=unwatched (default true)"},
        }, "required": ["airing_id"]},
        "safety": Safety.SAFE,
        "handler": sagetv_set_watched,
    },
    "sagetv_set_archived": {
        "description": "Archive (protect from auto-delete) or unarchive a recording.",
        "input_schema": {"type": "object", "properties": {
            "media_file_id": {"type": "string", "description": "The MediaFile ID"},
            "archived": {"type": "boolean", "description": "True=archive, false=unarchive (default true)"},
        }, "required": ["media_file_id"]},
        "safety": Safety.CONFIRM,
        "handler": sagetv_set_archived,
    },
    "sagetv_set_media_file_property": {
        "description": "Set a custom metadata property on a MediaFile (e.g. transcript_path, embedding_id, summary_version).",
        "input_schema": {"type": "object", "properties": {
            "media_file_id": {"type": "string", "description": "The MediaFile ID"},
            "key": {"type": "string", "description": "Property key name"},
            "value": {"type": "string", "description": "Property value"},
        }, "required": ["media_file_id", "key", "value"]},
        "safety": Safety.CONFIRM,
        "handler": sagetv_set_media_file_property,
    },
    "sagetv_get_media_file_property": {
        "description": "Get a custom metadata property from a MediaFile.",
        "input_schema": {"type": "object", "properties": {
            "media_file_id": {"type": "string", "description": "The MediaFile ID"},
            "key": {"type": "string", "description": "Property key name"},
        }, "required": ["media_file_id", "key"]},
        "safety": Safety.SAFE,
        "handler": sagetv_get_media_file_property,
    },

    # ---- Events ----
    "sagetv_subscribe_events": {
        "description": "Subscribe to recording lifecycle events. Events are pushed as JSON-RPC notifications on the same connection. Types: recording.started, recording.completed, recording.updated, recording.deleted.",
        "input_schema": {"type": "object", "properties": {
            "events": {"type": "array", "items": {"type": "string", "enum": [
                "recording.started", "recording.completed", "recording.updated", "recording.deleted", "*"
            ]}, "description": "Event types to subscribe to. Use '*' for all events."},
        }, "required": ["events"]},
        "safety": Safety.SAFE,
        "handler": _event_stub,
    },
    "sagetv_unsubscribe_events": {
        "description": "Unsubscribe from recording lifecycle events.",
        "input_schema": {"type": "object", "properties": {
            "events": {"type": "array", "items": {"type": "string"}, "description": "Event types to unsubscribe from. Omit to unsubscribe from all."},
        }},
        "safety": Safety.SAFE,
        "handler": _event_stub,
    },
}
