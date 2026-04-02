"""
tools.py
Complete MCP tool registry for SageTV.

Each tool maps to a sagex-api command per Appendix A / Appendix G.
Tools are namespaced with sagetv_ prefix.
"""

from __future__ import annotations
import enum
import logging
from typing import Any, Callable, Coroutine, Dict

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
    return _ok(data=data, message="Recordings retrieved")


async def sagetv_get_upcoming_recordings(client: SageXClient, args: Dict) -> Dict:
    data = await client.call("GetScheduledRecordings")
    return _ok(data=data, message="Upcoming recordings retrieved")


async def sagetv_get_channels(client: SageXClient, args: Dict) -> Dict:
    data = await client.call("GetAllChannels")
    return _ok(data=data, message="Channels retrieved")


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
        "description": "List upcoming scheduled recordings.",
        "input_schema": {"type": "object", "properties": {}},
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
}
