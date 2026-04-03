"""
tools.py
Complete MCP tool registry for Channels DVR.

Each tool maps to a Channels DVR REST API endpoint per Appendix B / Appendix G.
Tools are namespaced with channels_ prefix.
"""

from __future__ import annotations
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


# ==================================================================
# Playback tool handlers
# ==================================================================

async def _pause_playback(client, args: Dict) -> Dict:
    sid = args.get("session_id")
    if not sid:
        return _fail("missing_param", "session_id is required")
    await client.post(f"/dvr/sessions/{sid}/pause")
    return _ok(message="Playback paused")


async def _resume_playback(client, args: Dict) -> Dict:
    sid = args.get("session_id")
    if not sid:
        return _fail("missing_param", "session_id is required")
    await client.post(f"/dvr/sessions/{sid}/play")
    return _ok(message="Playback resumed")


async def _stop_playback(client, args: Dict) -> Dict:
    sid = args.get("session_id")
    if not sid:
        return _fail("missing_param", "session_id is required")
    await client.post(f"/dvr/sessions/{sid}/stop")
    return _ok(message="Playback stopped")


async def _seek_relative(client, args: Dict) -> Dict:
    sid = args.get("session_id")
    seconds = args.get("seconds", 0)
    if not sid:
        return _fail("missing_param", "session_id is required")
    await client.post(f"/dvr/sessions/{sid}/seek", json_body={"offset": seconds})
    return _ok(message=f"Seeked {seconds}s relative")


async def _seek_absolute(client, args: Dict) -> Dict:
    sid = args.get("session_id")
    position = args.get("position_seconds", 0)
    if not sid:
        return _fail("missing_param", "session_id is required")
    await client.post(f"/dvr/sessions/{sid}/seek", json_body={"position": position})
    return _ok(message=f"Seeked to {position}s")


async def _skip_commercial(client, args: Dict) -> Dict:
    sid = args.get("session_id")
    if not sid:
        return _fail("missing_param", "session_id is required")
    await client.post(f"/dvr/sessions/{sid}/commercial/next")
    return _ok(message="Skipped to next commercial break")


async def _previous_commercial(client, args: Dict) -> Dict:
    sid = args.get("session_id")
    if not sid:
        return _fail("missing_param", "session_id is required")
    await client.post(f"/dvr/sessions/{sid}/commercial/prev")
    return _ok(message="Returned to previous commercial break")


async def _set_playback_speed(client, args: Dict) -> Dict:
    sid = args.get("session_id")
    rate = args.get("rate", 1.0)
    if not sid:
        return _fail("missing_param", "session_id is required")
    await client.post(f"/dvr/sessions/{sid}/speed", json_body={"rate": rate})
    return _ok(message=f"Playback speed set to {rate}x")


# ==================================================================
# Query tool handlers
# ==================================================================

async def _get_now_playing(client, args: Dict) -> Dict:
    sessions = await client.get_sessions()
    return _ok(data=sessions, message=f"{len(sessions)} active sessions")


async def _get_recordings(client, args: Dict) -> Dict:
    recordings = await client.get_recordings()
    limit = args.get("limit", 50)
    return _ok(data=recordings[:limit], message=f"{len(recordings)} recordings total, returning {min(limit, len(recordings))}")


async def _get_scheduled_recordings(client, args: Dict) -> Dict:
    rules = await client.get_rules()
    return _ok(data=rules, message=f"{len(rules)} recording rules")


async def _get_channels(client, args: Dict) -> Dict:
    channels = await client.get_channels()
    return _ok(data=channels, message=f"{len(channels)} channels")


async def _search_epg(client, args: Dict) -> Dict:
    query = args.get("query", "")
    if not query:
        return _fail("missing_param", "query is required")
    results = await client.search_epg(query)
    return _ok(data=results, message=f"{len(results)} results for '{query}'")


async def _get_storage_status(client, args: Dict) -> Dict:
    dvr = await client.dvr_info()
    return _ok(data={
        "path": dvr.get("path", ""),
        "extra_paths": dvr.get("extra_paths", []),
        "disk": dvr.get("disk", {}),
        "stats": dvr.get("stats", {}),
    }, message="Storage status retrieved")


async def _get_jobs(client, args: Dict) -> Dict:
    jobs = await client.get_jobs()
    return _ok(data=jobs, message=f"{len(jobs)} jobs")


async def _get_clients(client, args: Dict) -> Dict:
    clients = await client.get_clients()
    return _ok(data=clients, message=f"{len(clients)} clients")


# ==================================================================
# Recording tool handlers
# ==================================================================

async def _schedule_recording(client, args: Dict) -> Dict:
    body = {
        "ProgramID": args.get("program_id"),
        "Channel": args.get("channel"),
        "StartTime": args.get("start_time"),
        "EndTime": args.get("end_time"),
    }
    result = await client.post("/dvr/rules", json_body=body)
    return _ok(data=result, message="Recording scheduled")


async def _schedule_series_recording(client, args: Dict) -> Dict:
    body = {
        "SeriesID": args.get("series_id"),
        "Channel": args.get("channel"),
    }
    options = args.get("options")
    if options:
        body.update(options)
    result = await client.post("/dvr/rules", json_body=body)
    return _ok(data=result, message="Series recording scheduled")


async def _cancel_scheduled_recording(client, args: Dict) -> Dict:
    rule_id = args.get("id")
    if not rule_id:
        return _fail("missing_param", "id is required")
    await client.delete(f"/dvr/rules/{rule_id}")
    return _ok(message=f"Recording rule {rule_id} cancelled")


async def _delete_recording(client, args: Dict) -> Dict:
    file_id = args.get("id")
    if not file_id:
        return _fail("missing_param", "id is required")
    await client.delete(f"/dvr/files/{file_id}")
    return _ok(message=f"Recording {file_id} deleted")


async def _delete_recording_file(client, args: Dict) -> Dict:
    file_id = args.get("id")
    if not file_id:
        return _fail("missing_param", "id is required")
    await client.delete(f"/dvr/files/{file_id}", params={"delete": "true"})
    return _ok(message=f"Recording file {file_id} permanently deleted")


# ==================================================================
# Commercial tool handlers
# ==================================================================

async def _regenerate_commercial_markers(client, args: Dict) -> Dict:
    file_id = args.get("id")
    if not file_id:
        return _fail("missing_param", "id is required")
    await client.post(f"/dvr/files/{file_id}/commercials/rebuild")
    return _ok(message=f"Commercial markers regeneration started for {file_id}")


# ==================================================================
# System tool handlers (OWNER)
# ==================================================================

async def _clear_cache(client, args: Dict) -> Dict:
    await client.post("/dvr/cache/clear")
    return _ok(message="Cache cleared")


async def _rebuild_index(client, args: Dict) -> Dict:
    await client.post("/dvr/index/rebuild")
    return _ok(message="Index rebuild started")


# ==================================================================
# Tool registry
# ==================================================================

TOOL_REGISTRY = {
    # --- Playback (SAFE) ---
    "channels_pause_playback": {
        "description": "Pause playback on a Channels DVR session.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Active session ID"}},
            "required": ["session_id"],
        },
        "safety": Safety.SAFE,
        "handler": _pause_playback,
    },
    "channels_resume_playback": {
        "description": "Resume playback on a Channels DVR session.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Active session ID"}},
            "required": ["session_id"],
        },
        "safety": Safety.SAFE,
        "handler": _resume_playback,
    },
    "channels_stop_playback": {
        "description": "Stop playback and close the media player.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Active session ID"}},
            "required": ["session_id"],
        },
        "safety": Safety.SAFE,
        "handler": _stop_playback,
    },
    "channels_seek_relative": {
        "description": "Seek forward or backward by a number of seconds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "seconds": {"type": "integer", "description": "Positive = forward, negative = backward"},
            },
            "required": ["session_id", "seconds"],
        },
        "safety": Safety.SAFE,
        "handler": _seek_relative,
    },
    "channels_seek_absolute": {
        "description": "Seek to an absolute position in seconds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "position_seconds": {"type": "integer", "description": "Target position in seconds"},
            },
            "required": ["session_id", "position_seconds"],
        },
        "safety": Safety.SAFE,
        "handler": _seek_absolute,
    },
    "channels_skip_commercial": {
        "description": "Skip to the end of the current commercial break.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
        "safety": Safety.SAFE,
        "handler": _skip_commercial,
    },
    "channels_previous_commercial": {
        "description": "Jump back to the previous commercial marker.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
        "safety": Safety.SAFE,
        "handler": _previous_commercial,
    },
    "channels_set_playback_speed": {
        "description": "Set the playback speed multiplier.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "rate": {"type": "number", "description": "Speed multiplier (e.g. 1.5, 2.0)"},
            },
            "required": ["session_id", "rate"],
        },
        "safety": Safety.SAFE,
        "handler": _set_playback_speed,
    },

    # --- Queries (SAFE) ---
    "channels_get_now_playing": {
        "description": "Get all active playback sessions.",
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
        "description": "Get all DVR jobs (recording, comskip, transcode).",
        "inputSchema": {"type": "object", "properties": {}},
        "safety": Safety.SAFE,
        "handler": _get_jobs,
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
