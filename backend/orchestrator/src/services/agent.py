"""
agent.py
Agentic tool-calling loop for the LLM.
Parses tool calls from LLM output, executes them via MCP clients,
and feeds results back until the LLM produces a final answer.
"""

from __future__ import annotations
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from utils.logger import get_logger
from services.planner_base import PlannerBase
from services.mcp_tool_registry import MCPToolRegistry

logger = get_logger(__name__)

MAX_ITERATIONS = 5  # default; overridden by config.agent.max_iterations


@dataclass
class RequestTrace:
    """Structured trace for a single agent request, logged as JSON on completion."""
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    query: str = ""
    temporal: str = ""
    domains: list = field(default_factory=list)
    tools_offered: int = 0
    steps: list = field(default_factory=list)  # [{tool, args_keys, duration_ms, result_size, error?}]
    iterations: int = 0
    model: str = ""
    total_ms: float = 0.0
    status: str = ""
    validation: str = ""  # "PASS" or "FAIL(reason)"
    validation_issues: list = field(default_factory=list)
    context_tokens_est: int = 0
    entity_count: int = 0

    def add_step(self, tool: str, args: dict, duration_ms: float,
                 result_size: int, error: str | None = None):
        step = {
            "tool": tool,
            "args_keys": list(args.keys()),
            "duration_ms": round(duration_ms, 1),
            "result_size": result_size,
        }
        if error:
            step["error"] = error[:200]
        self.steps.append(step)

    def log(self):
        logger.info(
            "REQUEST_TRACE %s",
            json.dumps({
                "trace_id": self.trace_id,
                "query": self.query[:100],
                "temporal": self.temporal,
                "domains": self.domains,
                "tools_offered": self.tools_offered,
                "steps": self.steps,
                "iterations": self.iterations,
                "model": self.model,
                "total_ms": round(self.total_ms, 1),
                "status": self.status,
                "validation": self.validation,
                "validation_issues": self.validation_issues,
                "context_tokens_est": self.context_tokens_est,
                "entity_count": self.entity_count,
            }, default=str),
        )


@dataclass
class ValidationResult:
    """Typed result from the post-hoc answer validator."""
    status: str  # "PASS" or "FAIL"
    issues: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def summary(self) -> str:
        if self.passed:
            return "PASS"
        return f"FAIL({'; '.join(self.issues[:3])})"


# Fields to strip from tool results before sending to the LLM.
# These are useful for the frontend popup but waste LLM context tokens.
_LLM_STRIP_FIELDS = {
    "cast", "genres", "image", "content_rating", "is_hd", "path",
    "description", "duration_min", "original_date", "channel",
}


def _slim_for_llm(obj):
    """Recursively strip frontend-only fields from tool results to save LLM tokens."""
    if isinstance(obj, dict):
        return {k: _slim_for_llm(v) for k, v in obj.items() if k not in _LLM_STRIP_FIELDS}
    if isinstance(obj, list):
        return [_slim_for_llm(item) for item in obj]
    return obj


def _truncate_result(obj, max_chars: int = 4000) -> str:
    """Truncate a tool result to fit within max_chars.

    Instead of slicing mid-JSON (which breaks parsing), this drops records
    from the end of lists until the serialized result fits.  Appends a
    '(N more omitted)' note so the LLM knows data was cut.
    """
    s = json.dumps(obj, default=str)
    if len(s) <= max_chars:
        return s

    # Find the largest list in the result and trim it
    def _find_list(d):
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for v in d.values():
                found = _find_list(v)
                if found is not None:
                    return found
        return None

    items = _find_list(obj)
    if items is None:
        # No list found — fall back to string truncation
        return s[:max_chars - 30] + "... (truncated)"

    original_len = len(items)
    while len(items) > 1:
        items.pop()
        s = json.dumps(obj, default=str)
        if len(s) <= max_chars - 40:
            omitted = original_len - len(items)
            # Inject note about omitted items
            s = s[:-1]  # remove final }
            s += f', "note": "{omitted} more results omitted"}}'
            return s

    # Even 1 item is too large — string truncate as last resort
    s = json.dumps(obj, default=str)
    return s[:max_chars - 30] + "... (truncated)"


def _count_items(result) -> int | None:
    """Count result items from a tool response for status display."""
    if not isinstance(result, dict):
        return None
    data = result.get("data", result)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        # Try common sub-keys: results, scheduled, items
        for k in ("results", "scheduled", "items", "recordings"):
            v = data.get(k)
            if isinstance(v, list):
                return len(v)
    return None

# ── Direct-format bypass for pure listing queries ──
# When all tools return structured recording lists, format directly in
# Python instead of burning an LLM iteration to reformat JSON → text.

# Tools whose results can be directly formatted as recording lists
_LISTING_TOOLS = {
    "channels_get_upcoming_recordings",
    "sagetv_get_upcoming_recordings",
    "channels_search_recordings",
    "sagetv_search_recordings",
    "sagetv_get_recordings",
    "sagetv_get_recent_recordings",
}


def _extract_recording_items(tool_name: str, result: dict) -> list[dict] | None:
    """Extract a flat list of recording dicts from a tool result.

    Returns None if the result isn't a simple recording list.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return None
    data = result.get("data")
    if data is None:
        return None
    # Channels upcoming: data = {"scheduled": [...], "skipped": [...]}
    if isinstance(data, dict):
        # Use explicit None checks — empty lists are valid results
        items = data.get("scheduled")
        if items is None:
            items = data.get("results")
        if items is None:
            items = data.get("recordings")
        if isinstance(items, list):
            return items
        return None
    # SageTV: data = [...]
    if isinstance(data, list):
        return data
    return None


def _format_recording_line(idx: int, rec: dict) -> str:
    """Format a single recording dict into the standard numbered line."""
    title = rec.get("title", "")
    ep_title = rec.get("episode_title", "")

    # Season/episode: Channels uses season+episode ints, SageTV uses season_episode string
    se = rec.get("season_episode", "")
    if not se:
        s = rec.get("season")
        e = rec.get("episode")
        if isinstance(s, int) and isinstance(e, int):
            se = f"S{s:02d}E{e:02d}"

    air_date = rec.get("air_date", "")
    watched = rec.get("watched", rec.get("is_watched", False))
    watched_str = "Watched" if watched else "Unwatched"
    status = rec.get("status", "available")

    parts = [f'{idx}. "{title}"']
    if ep_title:
        parts.append(f'"{ep_title}"')
    if se:
        parts.append(se)
    if air_date:
        parts.append(f"— {air_date}")
    parts.append(watched_str)
    if status == "watched_and_removed":
        parts.append("[Removed]")
    elif status == "failed":
        parts.append("[Failed]")
    elif status == "archived":
        parts.append("[Archived]")
    return " ".join(parts)


def _try_direct_format(tool_calls_executed: list[tuple[str, dict]],
                       tool_results: dict[str, dict]) -> str | None:
    """Attempt to directly format tool results as a recording list.

    Args:
        tool_calls_executed: List of (tool_name, args) executed this iteration.
        tool_results: Map of call_key -> result dict.

    Returns:
        Formatted string if direct-format applies, None to fall back to LLM.
    """
    # Only apply if ALL executed tools are listing tools
    if not tool_calls_executed:
        return None
    for tool_name, _args in tool_calls_executed:
        if tool_name not in _LISTING_TOOLS:
            return None

    # Collect all recording items
    all_items: list[dict] = []
    seen_keys: set[str] = set()  # dedup by title + episode_title + air_date

    for call_key, result in tool_results.items():
        items = _extract_recording_items(call_key.split(":")[0], result)
        if items is None:
            return None  # Non-list result — fall back to LLM
        for item in items:
            dedup_key = (
                (item.get("title", "") + "|" +
                 item.get("episode_title", "") + "|" +
                 item.get("air_date", item.get("start_time", ""))).lower()
            )
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                all_items.append(item)
        # Also include failed recordings from Channels search
        if isinstance(result, dict) and result.get("success"):
            data = result.get("data")
            if isinstance(data, dict):
                for fr in data.get("failed_recordings", []):
                    dedup_key = (fr.get("title", "") + "|" +
                                 fr.get("episode_title", "") + "|" +
                                 fr.get("start_time", "")).lower()
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        all_items.append(fr)

    if not all_items:
        return "No recordings found."

    # Sort by start_time or air_date for chronological order
    def _sort_key(r):
        return r.get("start_time", "") or r.get("air_date", "")
    all_items.sort(key=_sort_key)

    lines = [_format_recording_line(i + 1, rec) for i, rec in enumerate(all_items)]

    # Add summary counts
    n_avail = sum(1 for r in all_items if r.get("status", "available") == "available")
    n_removed = sum(1 for r in all_items if r.get("status") == "watched_and_removed")
    n_failed = sum(1 for r in all_items if r.get("status") == "failed")
    if n_removed or n_failed:
        summary_parts = [f"{n_avail} available on the DVR"]
        if n_removed:
            summary_parts.append(f"{n_removed} removed")
        if n_failed:
            summary_parts.append(f"{n_failed} failed")
        lines.append(f"\n({', '.join(summary_parts)})")
    else:
        lines.append("\n(Only recordings still on the DVR can be reported.)")

    return "\n".join(lines)


# Tool definitions split by system for filtering based on user's LLM Focus selection.
# Keys: "sagetv", "channelsdvr", "shared" (linux + transcript — always included)
# COMPACT format: tool(required_param, optional_param?) — description
_TOOL_SECTIONS = {
    "sagetv": """
## SageTV Tools
PAST: sagetv_search_recordings(title?, episode_title?, actor?, genre?, channel?, season?, episode?, start_date? YYYY-MM-DD, end_date? YYYY-MM-DD, watched? bool, limit?) | sagetv_get_recordings(limit?, offset?) | sagetv_get_recent_recordings(limit?) | sagetv_get_recording(media_file_id)
PRESENT: sagetv_get_active_recordings() | sagetv_get_now_playing()
FUTURE: sagetv_get_upcoming_recordings() | sagetv_search_shows(query)
ANY: sagetv_get_airing(airing_id) | sagetv_get_show(show_id) | sagetv_list_genres()
System: sagetv_get_channels() | sagetv_get_channel(station_id) | sagetv_get_disk_space() | sagetv_get_tuner_status() | sagetv_get_clients()
Playback: sagetv_pause_playback() | sagetv_resume_playback() | sagetv_stop_playback() | sagetv_skip_forward() | sagetv_skip_back() | sagetv_seek_relative(seconds) | sagetv_seek_absolute(position_seconds) | sagetv_set_volume(level) | sagetv_mute() | sagetv_unmute() | sagetv_commercial_skip() | sagetv_tune_channel(channel)
Nav: sagetv_open_recordings() | sagetv_open_guide() | sagetv_open_home() | sagetv_open_live_tv()
Manage: sagetv_record_show(airing_id) | sagetv_cancel_recording(airing_id) | sagetv_delete_media_file(media_file_id) | sagetv_set_watched(airing_id, watched?) | sagetv_set_archived(media_file_id, archived?)
Favorites: sagetv_create_favorite(title, channel?) | sagetv_remove_favorite(favorite_id)
Config: sagetv_get_config_value(key) | sagetv_set_config_value(key, value) | sagetv_run_library_scan()
""",

    "channelsdvr": """
## Channels DVR Tools
PAST: channels_search_recordings(title?, episode_title?, actor?, genre?, channel?, season?, episode?, start_date? YYYY-MM-DD, end_date? YYYY-MM-DD, watched? bool, limit?) — completed recordings on disk. Use watched=false for unwatched, watched=true for watched, omit for all
PAST: channels_get_recordings(limit?) — all saved recordings
FUTURE: channels_get_upcoming_recordings(date? YYYY-MM-DD, start_date? YYYY-MM-DD, end_date? YYYY-MM-DD, title?, channel?) — episodes scheduled to record. Use date for a single day (defaults to today), or start_date+end_date for a range
PRESENT: channels_get_now_playing() — what is airing live right now
FUTURE: channels_search_epg(query) — search the electronic program guide for upcoming shows
FUTURE: channels_get_scheduled_recordings() — recording RULES/passes, NOT actual recordings
ANY: channels_list_genres() — list all distinct genres with counts
Info: channels_get_channels() | channels_get_storage_status() | channels_get_jobs(status? 'active'|'completed'|'failed') | channels_get_clients()
Playback: channels_get_bridge_devices() | channels_get_playback_status(device?) | channels_pause_playback(device?) | channels_resume_playback(device?) | channels_toggle_pause(device?) | channels_stop_playback(device?) | channels_skip_commercial(device?) | channels_seek_relative(seconds, device?) | channels_seek_forward(device?) | channels_seek_backward(device?) | channels_toggle_mute(device?) | channels_toggle_cc(device?) | channels_play_channel(channel_number, device?) | channels_play_recording(recording_id, device?) | channels_channel_up(device?) | channels_channel_down(device?)
Manage: channels_schedule_recording(program_id, channel) | channels_schedule_series_recording(series_id, channel?) | channels_cancel_scheduled_recording(id) | channels_delete_recording(id) | channels_delete_recording_file(id) | channels_regenerate_commercial_markers(id)
System: channels_clear_cache() | channels_rebuild_index()
""",

    "shared": """
## Linux Tools (PRESENT)
Info: linux_disk_usage() | linux_memory_info() | linux_uptime() | linux_network_info()
Files: linux_list_directory(path) | linux_file_info(path) | linux_count_files(root, pattern) | linux_find_large_files(root, sort_by?, extension?)
Services: linux_service_status(service_name) | linux_restart_service(service_name) | linux_docker_ps() | linux_docker_restart(container) | linux_docker_logs(container, lines?) | linux_tail_log(path, lines?)
Danger: linux_reboot_server() | linux_shutdown_server() | linux_restart_nginx()

## Transcript Tools (PAST ONLY — transcripts cannot exist for unaired content)
transcript_cross_search(query, actor?, genre?, channel?, date_from?, date_to?, limit?) — FULL-TEXT search of transcript dialogue/spoken words. Use for "who said X", "what episode mentioned Y", finding quotes.
transcript_search(query, limit?) — search transcript metadata (titles/episodes). Use for "does X have a transcript".
transcript_list_recent(limit?) — list recordings that have transcripts, newest first. Use for "what recordings have transcripts".
transcript_actors(actor_name, limit?) | transcript_stats() | transcript_get(recording_id) | transcript_recording_summary(recording_id) | transcript_jobs(status?) | transcript_reindex(directory?)
""",
}


def _build_tool_definitions(systems: list[str] | None = None) -> str:
    """Build tool definitions text filtered by the active systems (static fallback)."""
    parts = ["## Available Tools\n"]
    all_systems = {"sagetv", "channelsdvr"}
    active = set(systems) if systems else all_systems
    if "sagetv" in active:
        parts.append(_TOOL_SECTIONS["sagetv"])
    if "channelsdvr" in active:
        parts.append(_TOOL_SECTIONS["channelsdvr"])
    parts.append(_TOOL_SECTIONS["shared"])
    return "".join(parts)


def _schema_to_compact(name: str, schema: Dict, description: str = "") -> str:
    """Convert a JSON Schema tool definition to compact one-liner format.

    Example: tool_name(required, optional?)
    Description is only appended when explicitly provided.
    """
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    params = []
    for pname, pdef in props.items():
        if pname.startswith("_"):
            continue  # skip internal params like _confirmed
        suffix = "" if pname in required else "?"
        pdesc = pdef.get("description", "")
        fmt = pdef.get("format", "")
        # Add format hint for dates
        hint = ""
        if "YYYY-MM-DD" in pdesc or fmt == "date":
            hint = " YYYY-MM-DD"
        params.append(f"{pname}{suffix}{hint}")
    param_str = ", ".join(params)
    base = f"{name}({param_str})"
    return f"{base} — {description}" if description else base


# Hints for key tools that need disambiguation in the prompt
_KEY_TOOL_HINTS: Dict[str, str] = {
    "channels_search_recordings": "USE THIS to find what was recorded on a date, by actor, genre, etc.",
    "channels_get_upcoming_recordings": "USE THIS for 'what's recording today/tonight/this week'",
    "channels_get_scheduled_recordings": "lists recording RULES/passes, NOT actual recordings",
    "channels_get_now_playing": "what is airing live right now",
    "channels_search_epg": "search program guide for upcoming shows",
    "channels_list_genres": "list all genres with counts",
    "sagetv_search_recordings": "search recordings by title, actor, genre, date, etc.",
    "sagetv_list_genres": "list all genres with counts",
}


def _categorize_tool(name: str) -> str:
    """Assign a display category to a tool based on its name."""
    for prefix in ("channels_", "sagetv_", "linux_", "transcript_"):
        if name.startswith(prefix):
            action = name[len(prefix):]
            break
    else:
        return "Other"

    if any(w in action for w in ("search", "get_recording", "get_recent", "get_active",
           "get_upcoming", "get_now_playing", "get_airing", "get_show")):
        return "Query"
    if any(w in action for w in ("play", "pause", "resume", "stop", "seek", "skip",
           "mute", "unmute", "volume", "commercial", "channel_up", "channel_down",
           "toggle", "tune")):
        return "Playback"
    if "open_" in action:
        return "Nav"
    if any(w in action for w in ("record", "cancel", "delete", "set_watched",
           "set_archived", "regenerate", "schedule", "favorite")):
        return "Manage"
    if any(w in action for w in ("config", "scan", "cache", "rebuild", "clear",
           "reindex")):
        return "Config"
    if any(w in action for w in ("get_channel", "get_disk", "get_storage", "get_tuner",
           "get_client", "get_job", "get_bridge", "get_playback_status",
           "get_scheduled", "disk_usage", "memory", "uptime", "network")):
        return "Info"
    if any(w in action for w in ("list_directory", "file_info", "count_files",
           "find_large")):
        return "Files"
    if any(w in action for w in ("service", "docker", "tail_log")):
        return "Services"
    if any(w in action for w in ("reboot", "shutdown", "restart")):
        return "Danger"
    return "Other"


# Human-readable status messages for tool calls shown in the UI
_TOOL_STATUS = {
    "channels_search_recordings": "Searching Channels DVR recordings",
    "channels_get_recordings": "Fetching Channels DVR recordings",
    "channels_get_now_playing": "Checking what's playing now",
    "channels_search_epg": "Searching the program guide",
    "channels_get_channels": "Getting channel lineup",
    "channels_get_storage_status": "Checking DVR storage",
    "channels_get_scheduled_recordings": "Getting scheduled recordings",
    "channels_get_upcoming_recordings": "Getting upcoming recordings",
    "channels_get_jobs": "Checking DVR jobs",
    "channels_get_clients": "Getting connected clients",
    "channels_list_genres": "Listing genres from Channels DVR",
    "sagetv_search_recordings": "Searching SageTV recordings",
    "sagetv_get_recordings": "Fetching SageTV recordings",
    "sagetv_get_recent_recordings": "Getting recent recordings",
    "sagetv_get_active_recordings": "Checking active recordings",
    "sagetv_get_upcoming_recordings": "Getting upcoming recordings",
    "sagetv_get_now_playing": "Checking what's playing now",
    "sagetv_search_shows": "Searching SageTV shows",
    "sagetv_list_genres": "Listing genres from SageTV",
    "transcript_search": "Searching transcripts",
    "transcript_cross_search": "Cross-searching transcripts",
    "transcript_stats": "Getting transcript stats",
    "linux_disk_usage": "Checking disk usage",
    "linux_memory_info": "Checking memory",
    "linux_uptime": "Checking system uptime",
    "linux_service_status": "Checking service status",
    "linux_docker_ps": "Listing containers",
}


def _tool_status_message(tool_name: str, tool_args: dict = None) -> str:
    """Return a human-readable status message for a tool call."""
    base = _TOOL_STATUS.get(tool_name)
    if not base:
        # Fallback: derive from tool name
        for prefix, system in [("channels_", "Channels DVR"), ("sagetv_", "SageTV"),
                               ("linux_", "system"), ("transcript_", "transcripts")]:
            if tool_name.startswith(prefix):
                action = tool_name[len(prefix):].replace("_", " ")
                if "play" in action or "pause" in action or "stop" in action or "seek" in action:
                    base = f"Controlling {system} playback"
                else:
                    base = f"Querying {system}"
                break
        if not base:
            base = f"Running {tool_name}"
    # Append relevant args for context
    if tool_args:
        hints = []
        for k in ("title", "query", "date", "start_date"):
            v = tool_args.get(k)
            if v:
                hints.append(str(v))
        if hints:
            base += f": {', '.join(hints)}"
    return base


class AgentLoop(PlannerBase):
    """Manages the tool-calling loop between the LLM and MCP servers."""

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator
        self._max_iterations = orchestrator.config.get("agent", {}).get(
            "max_iterations", MAX_ITERATIONS
        )
        self._dynamic_tools: Dict[str, str] | None = None  # cached dynamic tool text
        self._openai_tools: List[Dict[str, Any]] | None = None  # cached OpenAI-format tools
        self._tool_schemas: Dict[str, Dict[str, Any]] = {}  # cached schemas for validation
        self._tool_registry = MCPToolRegistry(orchestrator)

    # ------------------------------------------------------------------
    # OpenAI-format tool schema discovery
    # ------------------------------------------------------------------

    # Static transcript tool schemas (transcription MCP uses direct TCP,
    # but still has tools/list — we query it dynamically too, these are
    # the fallback definitions)
    _TRANSCRIPT_TOOLS_OPENAI: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "transcript_search",
                "description": "Full-text search across all transcripts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_cross_search",
                "description": "Cross-metadata transcript search. Searches transcript text with optional filters for actor, genre, channel, date range, system. If query is omitted/empty, lists transcripts matching the filters (e.g. all transcripts in a date range). Use date_from/date_to (YYYY-MM-DD) from the DATE REFERENCE block. For 'this week', use This week (Sun-Sat). For 'last week', use Last week (Sun-Sat).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Full-text search query (optional — omit to list all transcripts matching the filters)"},
                        "actor": {"type": "string", "description": "Filter by actor name"},
                        "genre": {"type": "string", "description": "Filter by genre"},
                        "channel": {"type": "string", "description": "Filter by channel name or number"},
                        "date_from": {"type": "string", "description": "Filter from date (YYYY-MM-DD) — use ONLY dates from the DATE REFERENCE block"},
                        "date_to": {"type": "string", "description": "Filter to date (YYYY-MM-DD) — use ONLY dates from the DATE REFERENCE block"},
                        "system": {"type": "string", "description": "Filter by system (sagetv or channelsdvr)"},
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_actors",
                "description": "Find recordings featuring a specific actor.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actor_name": {"type": "string", "description": "Actor name to search for"},
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["actor_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_stats",
                "description": "Get transcription subsystem statistics.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_get",
                "description": "Get transcript and metadata for a specific recording.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "recording_id": {"type": "string"},
                    },
                    "required": ["recording_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_recording_summary",
                "description": "Get full enriched summary for a recording.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "recording_id": {"type": "string"},
                    },
                    "required": ["recording_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_jobs",
                "description": "List transcription job queue status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Filter: pending/processing/done/error"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_list_recent",
                "description": "List the most recent transcripts ordered by date (newest first). Returns total count and details for each.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Number of transcripts to return (default 10)"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_reindex",
                "description": "Reindex all transcript sidecar files. CONFIRM before running.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory": {"type": "string", "description": "Directory to scan"},
                    },
                },
            },
        },
    ]

    @staticmethod
    def _mcp_to_openai_tool(t: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a single MCP tool definition to OpenAI function-calling format.

        MCP format:  {"name": ..., "description": ..., "inputSchema": {...}}
        OpenAI format: {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
        """
        schema = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": schema,
            },
        }

    # Tools to always include in OpenAI schemas (essential query tools).
    # Playback/Nav/Manage/Config/Danger tools consume too much context
    # and are rarely needed — they can be added later if requested.
    _ESSENTIAL_TOOLS = {
        # Channels DVR query tools
        "channels_search_recordings", "channels_get_recordings",
        "channels_get_upcoming_recordings", "channels_get_now_playing",
        "channels_search_epg", "channels_get_scheduled_recordings",
        "channels_get_channels", "channels_get_storage_status",
        "channels_get_jobs", "channels_get_clients",
        "channels_list_genres",
        # SageTV query tools
        "sagetv_search_recordings", "sagetv_get_recordings",
        "sagetv_get_recent_recordings", "sagetv_get_active_recordings",
        "sagetv_get_upcoming_recordings", "sagetv_get_now_playing",
        "sagetv_search_shows", "sagetv_get_channels",
        "sagetv_get_disk_space", "sagetv_get_tuner_status",
        "sagetv_get_clients", "sagetv_get_recording",
        "sagetv_get_airing", "sagetv_get_show",
        "sagetv_list_genres",
        # Linux
        "linux_disk_usage", "linux_memory_info", "linux_uptime",
        "linux_service_status", "linux_docker_ps",
        # Transcript
        "transcript_search", "transcript_cross_search",
        "transcript_actors", "transcript_stats",
        "transcript_get", "transcript_recording_summary",
        "transcript_list_recent",
    }

    # Domain → tool suffix mapping for subsetting
    _DOMAIN_TOOL_MAP: Dict[str, set[str]] = {
        "recordings": {
            "search_recordings", "get_recordings", "get_recent_recordings",
            "get_recording", "get_active_recordings", "get_airing", "get_show",
        },
        "schedule": {
            "get_upcoming_recordings", "get_scheduled_recordings",
            "search_epg", "get_now_playing", "search_shows",
        },
        "playback": {
            "pause_playback", "resume_playback", "stop_playback",
            "skip_forward", "skip_back", "seek_relative", "seek_absolute",
            "set_volume", "mute", "unmute", "commercial_skip",
            "toggle_pause", "seek_forward", "seek_backward",
            "skip_commercial", "previous_commercial", "toggle_mute",
            "toggle_cc", "play_channel", "play_recording",
            "channel_up", "channel_down", "get_bridge_devices",
            "get_playback_status", "get_now_playing", "tune_channel",
        },
        "system": {
            "disk_usage", "memory_info", "uptime", "network_info",
            "service_status", "docker_ps", "docker_logs", "docker_restart",
            "tail_log", "list_directory", "file_info", "count_files",
            "find_large_files", "get_disk_space", "get_tuner_status",
            "get_storage_status", "get_jobs", "get_clients",
        },
        "metadata": {
            "list_genres", "get_channels", "get_channel",
        },
        "transcript": {
            "search", "cross_search", "actors", "stats",
            "get", "recording_summary", "list_recent", "jobs", "reindex",
        },
    }

    def _tool_matches_domain(self, tool_name: str) -> bool:
        """Check if a tool matches the current domain classification."""
        domains = getattr(self, "_domains", [])
        if not domains:
            return True  # no filtering if no domains classified

        # Extract suffix: channels_search_recordings → search_recordings
        # transcript_search → search
        suffix = ""
        for prefix in ("channels_", "sagetv_", "linux_", "transcript_"):
            if tool_name.startswith(prefix):
                suffix = tool_name[len(prefix):]
                break

        for domain in domains:
            allowed = self._DOMAIN_TOOL_MAP.get(domain, set())
            if suffix in allowed:
                return True
        return False

    async def _discover_openai_tools(self, systems: list[str] | None = None) -> List[Dict[str, Any]]:
        """Build OpenAI-format tools using shared registry for all planners."""
        tools, schemas = await self._tool_registry.discover_openai_tools(
            systems=systems,
            domains=getattr(self, "_domains", []) or [],
            temporal=getattr(self, "_temporal", "") or "",
        )
        self._tool_schemas.update(schemas)
        logger.info("Total OpenAI-format tools: %d", len(tools))
        return tools

    async def _discover_tools(self, systems: list[str] | None = None) -> str:
        """Query each MCP server's tools/list and build compact grouped tool definitions.

        Produces compact output matching the static format: tools grouped by category
        with | separators, descriptions only for key tools that need disambiguation.
        Falls back to static _TOOL_SECTIONS if servers are unreachable.
        """
        from collections import defaultdict

        all_systems = {"sagetv", "channelsdvr"}
        active = set(systems) if systems else all_systems

        sections: Dict[str, str] = {}

        # Map system names to MCP clients and section headers
        server_map = []
        if "channelsdvr" in active and hasattr(self._orch, "_channels"):
            server_map.append(("channelsdvr", self._orch._channels, "Channels DVR"))
        if "sagetv" in active and hasattr(self._orch, "_sagetv"):
            server_map.append(("sagetv", self._orch._sagetv, "SageTV"))
        # Always include linux + transcript
        if hasattr(self._orch, "_linux"):
            server_map.append(("linux", self._orch._linux, "Linux"))

        # Ordered categories for output
        _CAT_ORDER = ("Query", "Info", "Playback", "Nav", "Manage", "Config",
                       "Files", "Services", "Danger", "Other")

        for sys_key, client, label in server_map:
            try:
                tools = await client.list_tools()
                # Group tools by category
                categories: Dict[str, list] = defaultdict(list)
                for t in tools:
                    schema = t.get("inputSchema", {})
                    hint = _KEY_TOOL_HINTS.get(t["name"], "")
                    compact = _schema_to_compact(t["name"], schema, hint)
                    cat = _categorize_tool(t["name"])
                    categories[cat].append(compact)

                lines = [f"\n## {label} Tools"]
                for cat in _CAT_ORDER:
                    if cat in categories:
                        lines.append(f"{cat}: {' | '.join(categories[cat])}")
                sections[sys_key] = "\n".join(lines) + "\n"
                logger.info("Discovered %d tools from %s", len(tools), label)
            except Exception as exc:
                logger.warning("Could not discover %s tools: %s — using static fallback", label, exc)
                # Fall back to static section
                fallback_key = {"channelsdvr": "channelsdvr", "sagetv": "sagetv", "linux": "shared"}.get(sys_key)
                if fallback_key and fallback_key in _TOOL_SECTIONS:
                    sections[sys_key] = _TOOL_SECTIONS[fallback_key]

        # Transcript tools always from static (direct TCP, no list_tools)
        if "linux" not in sections:
            sections["linux"] = _TOOL_SECTIONS["shared"]
        else:
            # Append transcript tools from static since they're on a separate server
            sections["linux"] += "\n## Transcript Tools\n" + "\n".join(
                line for line in _TOOL_SECTIONS["shared"].split("\n")
                if line.strip().startswith("transcript_")
            ) + "\n"

        parts = ["## Available Tools\n"]
        for key in ("channelsdvr", "sagetv", "linux"):
            if key in sections:
                parts.append(sections[key])
        return "".join(parts)

    async def _discover_system_paths(self) -> str:
        """Pre-discover dynamic paths from MCP servers for injection into the prompt."""
        lines = []
        try:
            storage = await self._orch._channels.call_tool(
                "channels_get_storage_status", {},
            )
            logger.info("Channels storage response: %s", str(storage)[:300])
            # Response is wrapped: {"success": ..., "data": {"path": ..., ...}}
            data = storage.get("data", storage)
            path = data.get("path", "")
            if path:
                lines.append(f"- Channels DVR recordings are stored at {path}\n")
                logger.info("Discovered Channels DVR path: %s", path)
            extra = data.get("extra_paths")
            if extra:
                for ep in extra:
                    lines.append(f"- Additional Channels DVR storage: {ep}\n")
        except Exception as exc:
            logger.warning("Could not discover Channels DVR path: %s", exc)
            lines.append(
                "- Channels DVR path is unknown (service may be offline). "
                "Try channels_get_storage_status if needed.\n"
            )
        return "".join(lines)

    async def _build_system_prompt(self, systems: list[str] | None = None) -> str:
        """Build the system prompt with dynamically discovered paths and unified routing rules."""
        import datetime
        now = datetime.datetime.now().astimezone()
        today = now.strftime("%A, %B %d, %Y")
        today_iso = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%I:%M %p %Z")
        yesterday = (now - datetime.timedelta(days=1))
        yesterday_str = yesterday.strftime("%A, %B %d, %Y")
        yesterday_iso = yesterday.strftime("%Y-%m-%d")
        week_ago = (now - datetime.timedelta(days=7))
        week_ago_iso = week_ago.strftime("%Y-%m-%d")
        # Week boundaries are Sunday -> Saturday (not rolling 7-day windows).
        days_since_sunday = (now.weekday() + 1) % 7
        this_week_start = now - datetime.timedelta(days=days_since_sunday)
        this_week_end = this_week_start + datetime.timedelta(days=6)
        last_week_start = this_week_start - datetime.timedelta(days=7)
        last_week_end = this_week_start - datetime.timedelta(days=1)
        this_week_start_iso = this_week_start.strftime("%Y-%m-%d")
        this_week_end_iso = this_week_end.strftime("%Y-%m-%d")
        last_week_start_iso = last_week_start.strftime("%Y-%m-%d")
        last_week_end_iso = last_week_end.strftime("%Y-%m-%d")
        channels_path_line = await self._discover_system_paths()

        # Determine which DVR systems are active
        all_systems = {"sagetv", "channelsdvr"}
        active = set(systems) if systems else all_systems
        has_sagetv = "sagetv" in active
        has_channels = "channelsdvr" in active
        both = has_sagetv and has_channels

        # Scope line
        if both:
            scope_line = "Both SageTV (sagetv_ tools) and Channels DVR (channels_ tools) are active."
        elif has_channels:
            scope_line = "Only Channels DVR is active. Use ONLY channels_ tools. Never mention SageTV."
        else:
            scope_line = "Only SageTV is active. Use ONLY sagetv_ tools. Never mention Channels DVR."

        # Search tool names
        search_tools = []
        if has_sagetv:
            search_tools.append("sagetv_search_recordings")
        if has_channels:
            search_tools.append("channels_search_recordings")

        # Pre-computed date reference block so the LLM doesn't do date math
        _DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday",
                       3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
        current_dow = now.weekday()
        # Build "last <day>" and "next <day>" references for all 7 days
        day_lines = []
        for dow, name in _DAY_NAMES.items():
            # Last occurrence (past)
            days_back = (current_dow - dow) % 7
            if days_back == 0:
                days_back = 7
            last_date = (now - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
            # Next occurrence (future, including today if same day)
            days_fwd = (dow - current_dow) % 7
            if days_fwd == 0:
                days_fwd = 7
            next_date = (now + datetime.timedelta(days=days_fwd)).strftime("%Y-%m-%d")
            day_lines.append(f"- Last {name}: {last_date}  |  Next {name}: {next_date}")
        day_ref = "\n".join(day_lines)

        date_block = (
            f"DATE REFERENCE (use these exact dates — do NOT calculate your own):\n"
            f"- Today: {today} = {today_iso}\n"
            f"- Yesterday: {yesterday_str} = {yesterday_iso}\n"
            f"- This week (Sunday-Saturday): {this_week_start_iso} to {this_week_end_iso}\n"
            f"- Last week (Sunday-Saturday): {last_week_start_iso} to {last_week_end_iso}\n"
            f"- Last 7 days (rolling): {week_ago_iso} to {today_iso}\n"
            f"{day_ref}\n"
            f"WHEN THE USER SAYS 'this week' — set date_from={this_week_start_iso} and date_to={this_week_end_iso}. "
            f"WHEN THE USER SAYS 'last week' — set date_from={last_week_start_iso} and date_to={last_week_end_iso}. "
            f"WHEN THE USER SAYS 'past week', 'recent', or 'lately' — set date_from={week_ago_iso} and date_to={today_iso}. "
            f"NEVER use any other date. NEVER use a year other than {today_iso[:4]}.\n"
        )

        return (
            f"You are an AI media assistant. Today: {today}, {current_time}.\n"
            f"{scope_line}\n\n"
            f"{date_block}\n"

            "MANDATORY TOOL USE:\n"
            "- You MUST call a tool to answer any question about recordings, schedules, playback, or system status.\n"
            "- You do NOT have access to live DVR data without calling a tool.\n"
            "- If you cannot determine which tool to use, respond with: \"I don't have enough information to answer that.\"\n"
            "- NEVER fabricate or guess DVR data. Only report what tools return.\n"
            "- You may ONLY use dates provided in the DATE REFERENCE block above. Do NOT invent or calculate dates yourself.\n"
            + ("- BOTH DVR systems are active. For ANY recording search, you MUST call BOTH "
               "channels_search_recordings AND sagetv_search_recordings in the same turn. "
               "Do NOT call only one — you will miss results.\n" if both else "")
            + "\n"

            "TEMPORAL RESOLUTION RULES:\n"
            "- 'recordings' (plural noun) = PAST. 'view recordings', 'list recordings', 'show recordings' → search_recordings.\n"
            "- Scheduling verbs (set, schedule, record, auto-record, subscribe) = FUTURE → get_upcoming_recordings.\n"
            "- 'is recording', 'recording now', 'currently', 'live', 'right now' = PRESENT → get_now_playing / get_active_recordings.\n"
            "- 'recorded today/yesterday/last week' (past tense) = PAST ONLY → search_recordings with dates.\n"
            "- 'records today/tonight/this week', 'what's scheduled', 'will record' = FUTURE ONLY → get_upcoming_recordings. Do NOT also check past.\n"
            "- 'what's on today' = BOTH past + future → call search_recordings AND get_upcoming_recordings.\n"
            "- If temporal intent is unclear, return BOTH past + future results.\n"
            "- Transcript tools are PAST ONLY — transcripts cannot exist for unaired content.\n"
            "- For 'who said X', 'what episode mentioned X', quotes, dialogue content → use transcript_cross_search (searches spoken words).\n"
            "- For 'what recordings have transcripts', 'recent transcripts' → use transcript_list_recent.\n"
            "- For 'does show X have a transcript' → use transcript_search with the show name.\n"
            "- NEVER use DVR recording search tools (channels_search_recordings, sagetv_search_recordings) for transcript questions.\n"
            "- 'play recording' / 'delete recording' = PAST (the recording already exists).\n"
            "- 'record this show' / 'set recording' = FUTURE (verb overrides noun).\n\n"

            "RULES:\n"
            "- When searching for a specific episode (e.g. 'Tracker S03E14 The Field Trip'), "
            "set title='Tracker' (the SHOW name), season=3, episode=14. "
            "NEVER put the episode title in the title field. NEVER guess a channel number.\n"
            "- If the exact episode is not found, list the episodes of that show that ARE available.\n"
            "- For upcoming/scheduled results, ALWAYS include the scheduled time from start_time so the user knows when each show airs.\n"
            "- Recordings include a 'watched' field. When listing past recordings, note which have been watched.\n"
            "- For date-based queries, ALWAYS call the DVR tool with start_date/end_date. Do not answer from context alone.\n"
            "- Use DVR tools for recordings, playback, EPG. Use linux_ tools only for filesystem/services.\n"
            "- For date searches, use start_date/end_date as YYYY-MM-DD in "
            + "/".join(search_tools) + ".\n"
            "- Never delete DVR files via linux_ tools. Use the DVR's delete tool.\n"
            "- Destructive tools require user confirmation first.\n"
            "- On tool error, tell the user briefly. Do NOT retry.\n"
            "- NEVER claim a transcript exists or is available unless you received transcript data from a transcript_ tool or from the pre-searched transcript excerpts in the context. "
            "If the user asks about transcripts and you have no transcript data, say 'No transcripts are available for these recordings.'\n"
            "- Final answers: plain language, concise. No JSON, no tool names, no code blocks, no IDs.\n"
            "- Never include server file paths or directory paths in answers.\n"
            "- Never describe your reasoning steps. Just give the answer.\n\n"

            "OUTPUT FORMAT:\n"
            "- List recordings as a numbered list, ONE line each. No bullet sub-items.\n"
            "- Each line MUST include the show name AND the episode title, both in quotes.\n"
            "- The show name comes from the 'title' field, the episode title from 'episode_title'.\n"
            "- Each line MUST end with the word Watched or Unwatched.\n"
            "- For multi-day queries, include the date on each line.\n"
            "- CORRECT single-day examples:\n"
            "  1. \"NCIS\" \"Toil and Trouble\" S23E19 Watched\n"
            "  2. \"The Floor\" \"Sister Act\" S05E05 Unwatched\n"
            "- CORRECT multi-day example:\n"
            "  1. \"NCIS\" \"Toil and Trouble\" S23E19 — Apr 28 Watched\n"
            "  2. \"Will Trent\" \"Cold Case\" S03E11 — Apr 27 Unwatched\n"
            "- WRONG (too many lines per entry):\n"
            "  1. **NCIS**\n"
            "     - Episode: \"Toil and Trouble\" S23E19\n"
            "     - Watched: No\n"
            "- NEVER use sub-bullets, extra lines, or bold **show names**.\n"
            "- You MUST list EVERY recording. Do NOT skip any.\n"
            "- Before the list, write a one-line summary count.\n"
            "- Do NOT include descriptions, channel numbers, or file paths.\n\n"

            "PATHS:\n"
            + ("- SageTV: /var/media/tv\n" if has_sagetv else "")
            + (channels_path_line if has_channels else "")
            + (
                "\n" + getattr(self, "_entity_store", None).format_context_for_prompt()
                if getattr(self, "_entity_store", None)
                and getattr(self, "_entity_store").format_context_for_prompt()
                else ""
            )
        )

    async def run(
        self,
        user_query: str,
        transcript_context: str = "",
        semantic_context: str = "",
        systems: list[str] | None = None,
        temporal: str = "",
        domains: list[str] | None = None,
        entity_store: Any | None = None,
        status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        token_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """
        Run the agentic loop: send query to LLM, parse tool calls,
        execute them, feed results back, repeat until final answer.

        Args:
            status_callback: Optional async callable to emit human-readable
                status messages (e.g. "Searching Channels DVR recordings").
            token_callback: Optional async callable to stream individual
                tokens to the frontend for progressive rendering.

        Returns:
            Dict with status, response, model, iterations.
        """
        # ── Pre-check: remove unreachable MCP servers from scope ──
        requested = set(systems) if systems else {"sagetv", "channelsdvr"}
        reachable: set[str] = set()
        mcp_map = {
            "sagetv": getattr(self._orch, "_sagetv", None),
            "channelsdvr": getattr(self._orch, "_channels", None),
        }
        for sys_name in requested:
            client = mcp_map.get(sys_name)
            if client and await client.ping():
                reachable.add(sys_name)
            else:
                logger.warning("Removing %s from query scope (MCP unreachable)", sys_name)
        if not reachable:
            return {
                "status": "error",
                "response": "All DVR services are currently offline. Please try again later.",
                "model": "",
                "iterations": 0,
            }
        systems = list(reachable)

        # Initialize request trace
        trace = RequestTrace(
            query=user_query,
            temporal=temporal or "",
            domains=domains or [],
        )
        trace_start = time.monotonic()

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": await self._build_system_prompt(systems)},
        ]
        logger.info("System prompt: %d chars", len(messages[0]["content"]))

        # Store active systems and temporal intent for tool-call guardrails
        self._active_systems = set(systems) if systems else {"sagetv", "channelsdvr"}
        self._empty_retries = 0  # track consecutive empty LLM responses
        self._temporal = temporal or ""
        self._seen_calls: set[str] = set()  # RAC: track (tool, args) to detect duplicates
        self._tool_results_cache: Dict[str, Any] = {}  # cache tool results for post-hoc validation
        self._domains = domains or []  # domain classification for tool subsetting
        self._entity_store = entity_store  # conversation-scoped entity memory

        _is_transcript_summary_query = self._is_transcript_summary_intent(user_query)

        llm_cfg = self._orch.config.get("llm", {})
        summary_params: Dict[str, Any] | None = None
        if _is_transcript_summary_query:
            # Keep summary generations fast and bounded for local models.
            summary_params = {
                "num_predict": int(llm_cfg.get("summary_num_predict", 640)),
                "temperature": float(llm_cfg.get("summary_temperature", 0.2)),
            }

        # Extract resolved dates from the rewritten query so we can
        # override wrong dates hallucinated by the LLM in tool args.
        # Patterns: "(2026-05-03)" or "(2026-05-03 to 2026-05-09)"
        _qdate_m = re.search(
            r"\((\d{4}-\d{2}-\d{2})(?:\s+to\s+(\d{4}-\d{2}-\d{2}))?\)\s*\??\s*$",
            user_query,
        )
        if _qdate_m:
            self._resolved_start = _qdate_m.group(1)
            self._resolved_end = _qdate_m.group(2)  # None for single-day
        else:
            self._resolved_start = None
            self._resolved_end = None

        # Discover OpenAI-format tool schemas for native tool calling
        openai_tools = await self._discover_openai_tools(systems)
        trace.tools_offered = len(openai_tools)
        logger.info("Passing %d tool schemas to Ollama (domains=%s)", len(openai_tools), self._domains)

        # Build user message with pre-fetched context
        context_parts = []
        # Skip semantic context for date-specific queries — the 7B model
        # confuses "relevant recordings from index" with actual query results
        # and filters by those titles instead of searching broadly.
        if semantic_context and temporal not in ("past",):
            context_parts.append(
                "Background context from media library (may not match the query date — "
                "always use tools to answer date-specific questions):\n"
                f"{semantic_context}"
            )
        if transcript_context:
            # If the pre-fetch produced an authoritative list of recent
            # transcripts (header starts with "Recent transcripts available"),
            # tell the LLM it has the complete answer and should not call
            # transcript_list_recent again.
            if transcript_context.startswith("Recent transcripts available"):
                context_parts.append(
                    "AUTHORITATIVE transcript inventory (this is the complete list — "
                    "do NOT call transcript_list_recent or transcript_search; "
                    "answer directly from this list, filtering by the date(s) "
                    "the user asked about):\n"
                    f"{transcript_context}"
                )
            elif transcript_context.startswith("No transcripts"):
                context_parts.append(
                    "AUTHORITATIVE transcript inventory (do NOT call any "
                    "transcript_ tool — answer directly from this):\n"
                    f"{transcript_context}"
                )
            else:
                context_parts.append(
                    "Relevant transcript excerpts (pre-searched for context):\n"
                    f"{transcript_context}"
                )

        if context_parts:
            user_content = (
                "\n\n".join(context_parts) + "\n\n"
                f"User question: {user_query}"
            )
        else:
            user_content = user_query

        messages.append({"role": "user", "content": user_content})

        llm_result: Dict[str, Any] = {}

        for iteration in range(self._max_iterations):
            logger.info("Agent loop iteration %d/%d", iteration + 1, self._max_iterations)

            if status_callback:
                if iteration == 0:
                    await status_callback("Thinking")
                else:
                    await status_callback("Analyzing results")

            # ── Context budget tracking ──
            # Rough estimate: 1 token ≈ 4 chars for English
            ctx_chars = sum(len(m.get("content", "")) for m in messages)
            # Also count tool schemas
            schema_chars = len(json.dumps(openai_tools, default=str)) if openai_tools else 0
            est_tokens = (ctx_chars + schema_chars) // 4
            ctx_limit = self._orch.llm.num_ctx  # typically 8192
            trace.context_tokens_est = est_tokens

            if est_tokens > ctx_limit * 0.85:
                logger.warning(
                    "Context budget tight: ~%d tokens estimated vs %d limit. Compressing history.",
                    est_tokens, ctx_limit,
                )
                messages = self._compress_history(messages)
                new_est = sum(len(m.get("content", "")) for m in messages) // 4
                logger.info("After compression: ~%d tokens (was ~%d)", new_est, est_tokens)
                trace.context_tokens_est = new_est

            # ── Call LLM with native tool schemas ──
            # On tool-call iterations we don't stream tokens to frontend
            # (the model returns structured tool_calls, not text).
            # On final-answer iterations we stream tokens.
            _forwarding = True
            _tool_detected = False
            _buffer: List[str] = []
            _buffer_len = 0
            _BUFFER_THRESHOLD = 1 if _is_transcript_summary_query else 20

            _think_active = False

            async def _on_token(token: str):
                nonlocal _forwarding, _tool_detected, _buffer, _buffer_len, _think_active
                if _tool_detected:
                    return
                _buffer.append(token)
                _buffer_len += len(token)
                buf_text = "".join(_buffer)
                # Suppress <think>...</think> blocks (qwen3 thinking mode)
                if not _think_active and "<think>" in buf_text:
                    _think_active = True
                if _think_active:
                    if "</think>" in buf_text:
                        _think_active = False
                        # Discard the thinking block, keep anything after </think>
                        after = buf_text.split("</think>", 1)[1].lstrip()
                        _buffer.clear()
                        _buffer_len = 0
                        if after:
                            _buffer.append(after)
                            _buffer_len = len(after)
                    return
                # With native tool calling, the model shouldn't output
                # <tool_call> tags, but check anyway for safety
                if re.search(r"<(?:tool|channel|function|api)_call>", buf_text) or "<tool" in buf_text:
                    _tool_detected = True
                    _forwarding = False
                    _buffer.clear()
                    if status_callback:
                        await status_callback("Calling tools")
                    return
                if _buffer_len >= _BUFFER_THRESHOLD and _forwarding and token_callback:
                    for t in _buffer:
                        await token_callback(t)
                    _buffer.clear()
                    _buffer_len = 0

            llm_result = await self._orch.llm.stream_chat(
                messages,
                token_callback=_on_token,
                tools=openai_tools,
                params=summary_params,
            )

            # Flush remaining buffered tokens (for non-tool-call responses)
            if _buffer and _forwarding and token_callback:
                for t in _buffer:
                    await token_callback(t)
                _buffer.clear()

            if llm_result.get("error"):
                trace.status = "error"
                trace.iterations = iteration + 1
                trace.total_ms = (time.monotonic() - trace_start) * 1000
                trace.log()
                return llm_result

            response_text = llm_result.get("response", "")
            native_tool_calls = llm_result.get("tool_calls", [])

            # ── Retry on completely empty LLM response (no text, no tools) ──
            # qwen3 with think=false sometimes produces nothing at all.
            if not response_text and not native_tool_calls:
                _empty_retries = getattr(self, "_empty_retries", 0) + 1
                self._empty_retries = _empty_retries
                if _empty_retries <= 2:
                    logger.warning(
                        "Empty LLM response (iter %d, retry %d) — retrying same messages",
                        iteration + 1, _empty_retries,
                    )
                    continue  # retry same iteration without appending to messages

            logger.info(
                "LLM response (iter %d, %d chars, %d native tool_calls): %s",
                iteration + 1, len(response_text),
                len(native_tool_calls), response_text[:200],
            )
            self._empty_retries = 0  # reset on non-empty response

            # ── Determine tool calls: prefer native, fall back to text parsing ──
            tool_calls: List[Dict[str, Any]] = []

            if native_tool_calls:
                # Convert Ollama's native format to our internal format
                for tc in native_tool_calls:
                    fn = tc.get("function", {})
                    tool_name = fn.get("name", "")
                    tool_args = fn.get("arguments", {})
                    if tool_name:
                        tool_calls.append({"tool": tool_name, "args": tool_args})
                logger.info("Using %d native tool calls", len(tool_calls))
            else:
                # Fallback: parse text-based <tool_call> tags (legacy/safety)
                tool_calls = self._parse_tool_calls(response_text)
                if tool_calls:
                    logger.info("Fell back to text-parsed tool calls: %d", len(tool_calls))

            # ── Dual-DVR guardrail ──
            # When both systems are active and the LLM only called one
            # *_search_recordings or *_get_upcoming_recordings, auto-inject
            # the mirror call for the other DVR so results are never one-sided.
            active = getattr(self, "_active_systems", set())
            if len(active) >= 2 and tool_calls:
                _MIRROR_SUFFIXES = {"search_recordings", "get_upcoming_recordings"}
                called_tools = {tc["tool"] for tc in tool_calls}
                for suffix in _MIRROR_SUFFIXES:
                    sage_tool = f"sagetv_{suffix}"
                    chan_tool = f"channels_{suffix}"
                    if sage_tool in called_tools and chan_tool not in called_tools:
                        # Copy args from the SageTV call
                        src = next(tc for tc in tool_calls if tc["tool"] == sage_tool)
                        tool_calls.append({"tool": chan_tool, "args": dict(src["args"])})
                        # Also inject into native_tool_calls so the role:tool
                        # message count stays consistent
                        if native_tool_calls:
                            native_tool_calls.append({
                                "function": {"name": chan_tool, "arguments": dict(src["args"])},
                            })
                        logger.info("Dual-DVR guardrail: auto-injected %s", chan_tool)
                    elif chan_tool in called_tools and sage_tool not in called_tools:
                        src = next(tc for tc in tool_calls if tc["tool"] == chan_tool)
                        tool_calls.append({"tool": sage_tool, "args": dict(src["args"])})
                        if native_tool_calls:
                            native_tool_calls.append({
                                "function": {"name": sage_tool, "arguments": dict(src["args"])},
                            })
                        logger.info("Dual-DVR guardrail: auto-injected %s", sage_tool)

            if not tool_calls:
                # No tool calls detected in any format
                if iteration == 0:
                    # Clarification branch: if the LLM asks a question instead
                    # of calling a tool, allow it — return as a clarification
                    stripped = response_text.strip()
                    if stripped.endswith("?") and len(stripped) < 500 and "\n" not in stripped:
                        logger.info("Iter 0 clarification question detected: %s", stripped[:100])
                        trace.status = "clarification"
                        trace.iterations = 1
                        trace.total_ms = (time.monotonic() - trace_start) * 1000
                        trace.log()
                        return {
                            "status": "clarification",
                            "response": self._strip_markers(stripped),
                            "model": llm_result.get("model", ""),
                            "iterations": 1,
                        }

                    # Iter 0: the LLM MUST call a tool for factual queries.
                    # Try to salvage tool name from prose first.
                    salvaged = self._salvage_tool_call(response_text)
                    if salvaged:
                        logger.info("Iter 0 salvaged tool call: %s", salvaged)
                        tool_calls = [salvaged]
                        # Fall through to tool-execution below
                    else:
                        logger.info("Iter 0 without tool call — forcing retry")
                        messages.append({"role": "assistant", "content": response_text})
                        messages.append({
                            "role": "user",
                            "content": (
                                "You MUST call a tool to answer this question. "
                                "You do NOT have access to live DVR data without a tool. "
                                "Call the appropriate tool now."
                            ),
                        })
                        continue
                else:
                    # Later iteration with no tool call = final answer
                    final = self._strip_markers(response_text)

                    if _is_transcript_summary_query:
                        _looks_wrong = bool(
                            re.search(r"(?im)^\s*\d+\.\s+.*\b(watched|unwatched)\b", final)
                            or re.search(r"\b(?:please\s+provide|unable\s+to\s+find|couldn't\s+find)\b", final, re.I)
                        )
                        if _looks_wrong:
                            cache = getattr(self, "_tool_results_cache", {}) or {}
                            summary_blob = None
                            transcript_blob = None
                            for _k, _v in cache.items():
                                if isinstance(_k, str) and _k.startswith("transcript_recording_summary:") and isinstance(_v, dict):
                                    summary_blob = _v
                                elif isinstance(_k, str) and _k.startswith("transcript_get:") and isinstance(_v, dict):
                                    transcript_blob = _v

                            if summary_blob or transcript_blob:
                                repair = await self._orch.llm.generate_chat(
                                    [
                                        {
                                            "role": "system",
                                            "content": (
                                                "You are a TV episode analyst. Produce ONLY a structured summary with headings and bullets: "
                                                "Episode Overview (2-3 sentences), Plot Breakdown (chronological major events), "
                                                "Key Characters, Important Dialogue and Turning Points, Themes and Story Arcs, "
                                                "Key Takeaways (5-8 bullets). Use transcript text as source of truth. "
                                                "Do NOT output a recording list. If missing, say 'Not shown in transcript.'"
                                            ),
                                        },
                                        {
                                            "role": "user",
                                            "content": (
                                                f"Question: {user_query}\n\n"
                                                f"transcript_recording_summary:\n{json.dumps(_slim_for_llm(summary_blob or {}), default=str)[:9000]}\n\n"
                                                f"transcript_get:\n{json.dumps(_slim_for_llm(transcript_blob or {}), default=str)[:12000]}"
                                            ),
                                        },
                                    ],
                                    params={"num_predict": 640, "temperature": 0.2},
                                )
                                repaired = self._strip_markers((repair or {}).get("response", ""))
                                if repaired and not re.search(r"(?im)^\s*\d+\.\s+.*\b(watched|unwatched)\b", repaired):
                                    final = repaired

                    # Post-hoc answer validation (Layer 3 — Formal Validator)
                    vr = self._validate_answer(final, user_query)
                    trace.validation = vr.summary()
                    if not vr.passed and not _is_transcript_summary_query:
                        final += f"\n\n_{vr.issues[0]}_"
                        trace.validation_issues = vr.issues
                    # Extract entities from tool results for conversation context
                    if hasattr(self, "_entity_store") and self._entity_store:
                        trace.entity_count = len(self._entity_store.entities)
                    trace.status = "ok"
                    trace.model = llm_result.get("model", "")
                    trace.iterations = iteration + 1
                    trace.total_ms = (time.monotonic() - trace_start) * 1000
                    trace.log()
                    return {
                        "status": "ok",
                        "response": final,
                        "model": llm_result.get("model", ""),
                        "iterations": iteration + 1,
                        "streamed": True,
                    }

            # ── Build the assistant message for the conversation ──
            if native_tool_calls:
                # Native format: assistant message with tool_calls array
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": response_text or "",
                    "tool_calls": native_tool_calls,
                }
            else:
                # Text-parsed: strip fabricated text after last closing tag
                tc_match = list(re.finditer(r"</(?:tool|channel|function|api)_call>", response_text))
                if tc_match:
                    assistant_text = response_text[:tc_match[-1].end()]
                else:
                    assistant_text = response_text
                assistant_msg = {"role": "assistant", "content": assistant_text}

            messages.append(assistant_msg)

            # ── Execute tools and collect results ──
            tool_results: List[str] = []
            tool_messages: List[Dict[str, Any]] = []
            has_service_error = False
            _iter_calls: list[tuple[str, dict]] = []  # track (tool_name, args) for direct-format
            _iter_results: list[tuple[str, dict, dict]] = []  # track concrete tool results

            for idx, tc in enumerate(tool_calls):
                tool_name = tc.get("tool", "")
                tool_args = tc.get("args") or {}
                logger.info("Executing tool: %s(%s)", tool_name, json.dumps(tool_args, default=str)[:200])

                # ── RAC: Duplicate call detection ──
                call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, default=str)}"
                if call_key in self._seen_calls:
                    logger.warning("RAC: duplicate call %s — returning cached or error", tool_name)
                    result = self._tool_results_cache.get(call_key, {
                        "error": f"Already called {tool_name} with these parameters. Use the previous result."
                    })
                    trace.add_step(tool_name, tool_args, 0, 0, error="duplicate_call")
                else:
                    self._seen_calls.add(call_key)

                    if status_callback:
                        await status_callback(_tool_status_message(tool_name, tool_args))

                    step_start = time.monotonic()
                    result = await self._execute_tool(tool_name, tool_args)
                    step_ms = (time.monotonic() - step_start) * 1000
                    self._tool_results_cache[call_key] = result
                    # Extract entities from tool results for conversation context
                    if self._entity_store and isinstance(result, dict):
                        self._entity_store.extract_from_tool_result(tool_name, result)
                    result_str_raw = json.dumps(result, default=str)
                    trace.add_step(
                        tool_name, tool_args, step_ms, len(result_str_raw),
                        error=result.get("error"),
                    )
                    _iter_calls.append((tool_name, tool_args))
                    _iter_results.append((tool_name, tool_args, result))

                # ── Confirmation gate: return to user if confirmation needed ──
                if result.get("requires_confirmation"):
                    trace.status = "confirmation_required"
                    trace.iterations = iteration + 1
                    trace.total_ms = (time.monotonic() - trace_start) * 1000
                    trace.log()
                    return {
                        "status": "confirmation_required",
                        "response": result["message"],
                        "confirmation": {
                            "tool": result["tool"],
                            "args": result["args"],
                        },
                        "model": llm_result.get("model", ""),
                        "iterations": iteration + 1,
                    }

                result_slim = _slim_for_llm(result)
                result_str = json.dumps(result_slim, default=str)
                logger.info("Slimmed result for %s (%d chars): %.2000s", tool_name, len(result_str), result_str)

                # Send result-size status
                if status_callback:
                    n_items = _count_items(result_slim)
                    size_kb = len(result_str) / 1024
                    parts = []
                    if n_items is not None:
                        parts.append(f"{n_items} items")
                    parts.append(f"{size_kb:.1f} KB")
                    await status_callback(f"Got {', '.join(parts)}")

                if len(result_str) > 4000:
                    result_str = _truncate_result(result_slim, 4000)

                if result.get("error") and "unavailable" in str(result["error"]).lower():
                    has_service_error = True

                # Build role:tool message for native format
                if native_tool_calls and idx < len(native_tool_calls):
                    # Prefix with tool name so the LLM knows which source returned each result
                    labeled_result = f"[{tool_name}] {result_str}"
                    tool_messages.append({
                        "role": "tool",
                        "content": labeled_result,
                    })
                else:
                    tool_results.append(f"Tool: {tool_name}\nResult: {result_str}")

            # Transcript-summary helper: if the model only searched/listed,
            # auto-fetch transcript_recording_summary for the top hit.
            if _is_transcript_summary_query:
                already_has_summary = any(
                    n == "transcript_recording_summary" for n, _a, _r in _iter_results
                )
                if not already_has_summary:
                    candidate_ids: list[str] = []

                    def _collect_ids(_res: dict) -> list[str]:
                        out: list[str] = []
                        if not isinstance(_res, dict):
                            return out
                        data = _res.get("data", _res)
                        if isinstance(data, dict):
                            rid = data.get("recording_id")
                            if isinstance(rid, str) and rid:
                                out.append(rid)
                            for key in ("results", "recent", "recordings", "items"):
                                rows = data.get(key)
                                if isinstance(rows, list):
                                    for row in rows:
                                        if isinstance(row, dict):
                                            rr = row.get("recording_id")
                                            if isinstance(rr, str) and rr:
                                                out.append(rr)
                        return out

                    for _name, _args, _res in _iter_results:
                        if _name in {"transcript_cross_search", "transcript_search", "transcript_list_recent"}:
                            candidate_ids.extend(_collect_ids(_res))

                    # Deterministic fallback: when transcript summary was requested
                    # but tool results had no ids, fuzzy-match against recent
                    # transcript metadata using the user's title hint.
                    if not candidate_ids:
                        def _extract_summary_title(_q: str) -> str | None:
                            _m = re.search(
                                r"\b(?:summari[sz]e|summary|recap|what\s+happened)\b[^?]*\btranscript\b[^?]*"
                                r"\b(?:from|for|of)\b\s+(.+?)(?:\?|$)",
                                _q or "",
                                re.I,
                            )
                            if _m:
                                return _m.group(1).strip().strip('"\' .') or None
                            _m2 = re.search(r"\b(?:from|for|of)\b\s+(.+?)(?:\?|$)", _q or "", re.I)
                            return (_m2.group(1).strip().strip('"\' .') if _m2 else None) or None

                        _hint = _extract_summary_title(user_query)
                        if _hint:
                            recent_args = {"limit": 200}
                            recent_key = f"transcript_list_recent:{json.dumps(recent_args, sort_keys=True, default=str)}"
                            recent_result = self._tool_results_cache.get(recent_key)
                            if not recent_result:
                                recent_result = await self._execute_tool("transcript_list_recent", recent_args)
                                self._tool_results_cache[recent_key] = recent_result

                            rdata = recent_result.get("data", recent_result) if isinstance(recent_result, dict) else {}
                            rows = rdata.get("recent", []) if isinstance(rdata, dict) else []
                            if isinstance(rows, list) and rows:
                                import difflib as _difflib

                                def _norm(_s: str) -> str:
                                    _s = (_s or "").lower()
                                    _s = _s.replace("---", " ").replace("--", " ")
                                    _s = _s.replace("—", " ").replace("–", " ")
                                    _s = re.sub(r"[^a-z0-9\s]", " ", _s)
                                    return re.sub(r"\s+", " ", _s).strip()

                                seeds = [_hint] + self._query_variants(_hint)
                                best_row = None
                                best_score = 0.0
                                for row in rows:
                                    if not isinstance(row, dict):
                                        continue
                                    cand = " ".join([
                                        str(row.get("title") or ""),
                                        str(row.get("episode_title") or row.get("episode") or ""),
                                    ]).strip()
                                    cn = _norm(cand)
                                    if not cn:
                                        continue
                                    score = 0.0
                                    for seed in seeds:
                                        qn = _norm(seed)
                                        if not qn:
                                            continue
                                        qtokens = set(qn.split())
                                        ctokens = set(cn.split())
                                        ratio = _difflib.SequenceMatcher(None, qn, cn).ratio()
                                        overlap = len(qtokens & ctokens) / max(1, len(qtokens)) if qtokens else 0.0
                                        local_score = max(ratio, overlap)
                                        if qn in cn:
                                            local_score = max(local_score, 0.95)
                                        score = max(score, local_score)
                                    if score > best_score:
                                        best_score = score
                                        best_row = row

                                if best_row is not None and best_score >= 0.45:
                                    rid = best_row.get("recording_id")
                                    if isinstance(rid, str) and rid:
                                        candidate_ids.append(rid)

                    if candidate_ids:
                        rid = candidate_ids[0]
                        if status_callback:
                            await status_callback("Analyzing transcript")
                        summary_args = {"recording_id": rid}
                        summary_key = f"transcript_recording_summary:{json.dumps(summary_args, sort_keys=True, default=str)}"
                        summary_result = self._tool_results_cache.get(summary_key)
                        if not summary_result:
                            summary_result = await self._execute_tool("transcript_recording_summary", summary_args)
                            self._tool_results_cache[summary_key] = summary_result

                        summary_slim = _slim_for_llm(summary_result)
                        summary_str = json.dumps(summary_slim, default=str)
                        if len(summary_str) > 4000:
                            summary_str = _truncate_result(summary_slim, 4000)

                        if native_tool_calls:
                            tool_messages.append({
                                "role": "tool",
                                "content": f"[transcript_recording_summary] {summary_str}",
                            })
                        else:
                            tool_results.append(
                                "Tool: transcript_recording_summary\n"
                                f"Result: {summary_str}"
                            )

                        # Always pull transcript text for summary requests so the
                        # model can combine metadata + summary table + raw transcript.
                        get_args = {"recording_id": rid}
                        get_key = f"transcript_get:{json.dumps(get_args, sort_keys=True, default=str)}"
                        get_result = self._tool_results_cache.get(get_key)
                        if not get_result:
                            get_result = await self._execute_tool("transcript_get", get_args)
                            self._tool_results_cache[get_key] = get_result

                        # Keep enough transcript text for summarization, but cap payload.
                        get_data = get_result.get("data", get_result) if isinstance(get_result, dict) else {}
                        if isinstance(get_data, dict) and isinstance(get_data.get("transcript"), str):
                            txt = get_data.get("transcript", "")
                            if len(txt) > 12000:
                                get_data = dict(get_data)
                                get_data["transcript"] = txt[:12000] + "\n... (truncated)"
                                get_result = {"success": True, "data": get_data}

                        get_slim = _slim_for_llm(get_result)
                        get_str = json.dumps(get_slim, default=str)
                        if len(get_str) > 4000:
                            get_str = _truncate_result(get_slim, 4000)

                        if native_tool_calls:
                            tool_messages.append({
                                "role": "tool",
                                "content": f"[transcript_get] {get_str}",
                            })
                        else:
                            tool_results.append(
                                "Tool: transcript_get\n"
                                f"Result: {get_str}"
                            )

            # ── Direct-format bypass: skip LLM iteration 2 for pure listings ──
            if not has_service_error and not _is_transcript_summary_query and iteration == 0 and _iter_calls:
                direct = _try_direct_format(_iter_calls, self._tool_results_cache)
                if direct is not None:
                    logger.info("Direct-format bypass: %d chars, skipping LLM iteration 2",
                                len(direct))
                    if status_callback:
                        await status_callback("Formatting results")
                    # Stream the formatted text to frontend
                    if token_callback:
                        for line in direct.split("\n"):
                            await token_callback(line + "\n")
                    trace.status = "ok"
                    trace.iterations = iteration + 1
                    trace.total_ms = (time.monotonic() - trace_start) * 1000
                    trace.validation = "PASS"
                    trace.model = llm_result.get("model", "")
                    trace.log()
                    return {
                        "status": "ok",
                        "response": direct,
                        "model": llm_result.get("model", ""),
                        "iterations": iteration + 1,
                    }

            # ── Inject tool results back into conversation ──
            _FORMAT_REMINDER = (
                "REMINDER: List recordings as a numbered list, ONE line each. "
                "Format: 1. \"ShowName\" \"EpisodeTitle\" S##E## — air_date Unwatched\n"
                "You MUST include BOTH the show name AND the episode title from "
                "the tool results. The show name is in the 'title' field and the "
                "episode title is in the 'episode_title' field. Never omit either.\n"
                "You MUST include the 'air_date' value from each tool result on every line. "
                "Example: 1. \"FBI\" \"Roleplay\" S08E20 — Mon May 4 Unwatched\n"
                "End each line with the word Watched or Unwatched. "
                "Do NOT use sub-bullets or extra lines per entry."
            )
            _SUMMARY_REMINDER = (
                "REMINDER: The user asked for a transcript summary. "
                "Use transcript_recording_summary for metadata context and transcript_get for primary episode facts. "
                "If there is any conflict, trust transcript_get transcript text. "
                "The user already provided the title hint; do NOT ask for show name/episode title again. "
                "Return ONLY these sections with clear headings and bullets: "
                "1) Episode Overview (2-3 sentences), "
                "2) Plot Breakdown (chronological major events only), "
                "3) Key Characters (name + role + what they do in this episode), "
                "4) Important Dialogue and Turning Points (brief quotes only if meaningful), "
                "5) Themes and Story Arcs (themes + how arcs advance), "
                "6) Key Takeaways (5-8 bullets, Previously On style). "
                "Constraints: concise but complete; no filler/minor details; preserve relationships and causality; "
                "do NOT invent facts, names, motives, or events; if detail is missing, say 'Not shown in transcript.' "
                "Do NOT output a numbered recording list."
            )
            _FOLLOWUP_REMINDER = _SUMMARY_REMINDER if _is_transcript_summary_query else _FORMAT_REMINDER
            if tool_messages:
                # Native path: add each tool result as a role:tool message
                messages.extend(tool_messages)
                if has_service_error:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Some services are currently offline. "
                            "Do NOT retry the same tool. Tell the user which service "
                            "is unavailable and answer with whatever information you have. "
                            + _FOLLOWUP_REMINDER
                        ),
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": _FOLLOWUP_REMINDER,
                    })
            else:
                # Text-parsed fallback path
                observation = "\n\n".join(tool_results)
                if has_service_error:
                    observation += (
                        "\n\nIMPORTANT: Some services are currently offline. "
                        "Do NOT retry the same tool. Tell the user which service "
                        "is unavailable and answer with whatever information you have."
                    )
                messages.append({
                    "role": "user",
                    "content": (
                        f"Tool results:\n{observation}\n\n"
                        "Use these results to answer the original question. "
                        "If a tool returned an error, do NOT retry it — tell the user. "
                        "Otherwise, provide your final answer. "
                        + _FOLLOWUP_REMINDER
                    ),
                })

        # Max iterations reached
        logger.warning("Agent loop reached max iterations (%d)", self._max_iterations)
        raw = llm_result.get("response", "")
        final = self._strip_markers(raw)
        # Post-hoc validation on max-iterations path too
        vr = self._validate_answer(final, user_query)
        trace.validation = vr.summary()
        if not vr.passed:
            trace.validation_issues = vr.issues
        if hasattr(self, "_entity_store") and self._entity_store:
            trace.entity_count = len(self._entity_store.entities)
        trace.status = "max_iterations"
        trace.model = llm_result.get("model", "")
        trace.iterations = self._max_iterations
        trace.total_ms = (time.monotonic() - trace_start) * 1000
        trace.log()
        return {
            "status": "ok",
            "response": (
                "I wasn't able to fully resolve your request within the "
                "allowed steps. Here's what I found so far: "
                + final
            ),
            "model": llm_result.get("model", ""),
            "iterations": self._max_iterations,
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    # Accept <tool_call>, <channel_call>, <function_call>, etc.
    _TAG = r"<(?:tool|channel|function|api)_call>"
    _TAG_CLOSE = r"</(?:tool|channel|function|api)_call>"
    # Pattern 1: properly closed <*_call>...</*_call>
    _TOOL_CALL_RE = re.compile(
        _TAG + r"\s*(\{.*\})\s*" + _TAG_CLOSE, re.DOTALL,
    )
    # Pattern 2: unclosed <*_call> followed by JSON (common with mistral)
    _TOOL_CALL_OPEN_RE = re.compile(
        _TAG + r"\s*(\{[^<]+\})", re.DOTALL,
    )

    # Fallback: <tool_name>() or <tool_name>({...}) — bare tool-as-tag format
    _BARE_TOOL_RE = re.compile(
        r"<((?:sagetv|channels|linux|transcript)_\w+)>\s*\(?\s*(\{[^)]*\})?\s*\)?",
        re.DOTALL,
    )

    # Regex to extract tool names and date-like args from LLM prose
    _PROSE_TOOL_RE = re.compile(
        r"""\b((?:sagetv|channels|linux|transcript)_\w+)\b""",
    )
    _PROSE_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

    def _salvage_tool_call(self, text: str) -> Dict[str, Any] | None:
        """Try to extract a tool call from the LLM's natural-language description.

        When the LLM says 'I will use channels_search_recordings with
        start_date 2026-04-13', we can construct the call directly instead
        of wasting an iteration on a retry.
        """
        m = self._PROSE_TOOL_RE.search(text)
        if not m:
            return None
        tool_name = m.group(1)
        args: Dict[str, Any] = {}
        # Extract date args
        dates = self._PROSE_DATE_RE.findall(text)
        if dates and "search_recordings" in tool_name:
            args["start_date"] = dates[0]
            if len(dates) > 1:
                args["end_date"] = dates[1]
        elif dates and "upcoming" in tool_name:
            args["date"] = dates[0]
        return {"tool": tool_name, "args": args}

    def _parse_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        """Extract tool call JSON blocks from LLM response text.

        Handles: <tool_call>{...}</tool_call>, <tool_call>{...} (unclosed),
        <tool_name>() (bare tool-as-tag), and multiple tool calls.
        """
        # Try closed tags first, fall back to unclosed
        matches = self._TOOL_CALL_RE.findall(text)
        if not matches:
            matches = self._TOOL_CALL_OPEN_RE.findall(text)

        calls: List[Dict[str, Any]] = []
        for match in matches:
            raw = match.strip()
            json_str = self._extract_balanced_json(raw) or raw
            try:
                parsed = json.loads(json_str)
                # Accept both formats:
                # Ours:    {"tool": "name", "args": {...}}
                # Qwen2.5: {"name": "name", "arguments": {...}}
                tool_name = parsed.get("tool") or parsed.get("name")
                if tool_name:
                    tool_args = parsed.get("args") or parsed.get("arguments") or {}
                    calls.append({"tool": tool_name, "args": tool_args})
                else:
                    logger.warning("Tool call missing 'tool'/'name' key: %s", json_str[:200])
            except json.JSONDecodeError:
                logger.warning("Malformed tool call JSON: %s", json_str[:200])

        # Fallback: bare <tool_name>(args) format
        if not calls:
            for m in self._BARE_TOOL_RE.finditer(text):
                tool_name = m.group(1)
                args_json = m.group(2)
                args = {}
                if args_json:
                    try:
                        args = json.loads(args_json)
                    except json.JSONDecodeError:
                        pass
                logger.info("Parsed bare tool tag: %s(%s)", tool_name, args)
                calls.append({"tool": tool_name, "args": args})
        return calls

    @staticmethod
    def _extract_balanced_json(text: str) -> str | None:
        """Extract a balanced JSON object from text starting at the first '{'."""
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    @staticmethod
    def _strip_markers(text: str) -> str:
        """Remove stray tool_call tags and leaked tool artifacts from final text."""
        text = re.sub(r"</?(?:tool|channel|function|api)_call>", "", text)
        text = re.sub(r"</?tool_response>", "", text)
        # Remove bare <tool_name>() invocations
        text = re.sub(r"<(?:sagetv|channels|linux|transcript)_\w+>\s*\(?[^)]*\)?\s*", "", text)
        # Remove lines that look like leaked tool results
        text = re.sub(r"Tool:\s*\S+\s*Result:\s*\[.*?\]", "", text, flags=re.DOTALL)
        text = re.sub(r"Tool:\s*\S+\s*Result:\s*\{.*?\}", "", text, flags=re.DOTALL)
        return text.strip()

    @staticmethod
    def _is_transcript_summary_intent(query: str) -> bool:
        """Best-effort detector for transcript summary asks.

        Handles strict phrasing ("summarize transcript ...") and looser
        rewritten forms where "transcript" may be omitted.
        """
        q = (query or "").strip()
        if not q:
            return False
        ql = q.lower()
        if not re.search(r"\b(?:summari[sz]e|summary|recap|what\s+happened)\b", ql):
            return False
        if "transcript" in ql:
            return True
        return bool(re.search(r"\b(?:from|for|of)\b", ql) and re.search(r"\b(?:show|episode)\b", ql))

    # ------------------------------------------------------------------
    # Post-hoc answer validation (Layer 3 — Formal Validator)
    # ------------------------------------------------------------------

    _DATE_IN_ANSWER_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
    _COUNT_RE = re.compile(r"\b(\d+)\s+(?:recording|episode|show|result|item)s?\b", re.I)

    def _validate_answer(self, answer: str, user_query: str) -> ValidationResult:
        """Rule-based checks on the final answer against tool results.

        Returns a ValidationResult with PASS or FAIL status.
        """
        issues = []
        temporal = getattr(self, "_temporal", "")
        cache = getattr(self, "_tool_results_cache", {})

        # 1) Temporal direction check: past query shouldn't mention future dates
        if temporal == "past":
            import datetime
            today = datetime.date.today()
            for date_str in self._DATE_IN_ANSWER_RE.findall(answer):
                try:
                    d = datetime.date.fromisoformat(date_str)
                    if d > today:
                        issues.append(f"Note: Date {date_str} is in the future but the query asked about past content.")
                        break
                except ValueError:
                    pass

        # 2) Count consistency: check if claimed count matches tool results
        total_items = 0
        for key, result in cache.items():
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, list):
                    total_items += len(data)
                elif isinstance(data, dict):
                    for sub in ("results", "scheduled", "items", "recordings"):
                        v = data.get(sub)
                        if isinstance(v, list):
                            total_items += len(v)
                            break

        if total_items > 0:
            for m in self._COUNT_RE.finditer(answer):
                claimed = int(m.group(1))
                # Skip season/episode numbering (e.g. "Season 25 Episode 3")
                pre = answer[max(0, m.start() - 8):m.start()]
                if re.search(r'(?:Season|S)\s*$', pre, re.I):
                    continue
                # Allow some tolerance (the answer might group/filter)
                if claimed > total_items * 2 and claimed > total_items + 5:
                    issues.append(
                        f"Note: The answer mentions {claimed} items but tools returned {total_items}."
                    )
                    break

        # 3) Existence check: quoted show titles should appear in tool results
        # Skip this warning for transcript-summary queries; title variants
        # like "Show --- Episode" are common and can create false negatives.
        _is_transcript_summary_query = self._is_transcript_summary_intent(user_query)
        quoted_titles = re.findall(r'"([^"]{3,50})"', answer)
        _has_transcript_tool_results = any(str(k).startswith("transcript_") for k in cache.keys())
        if quoted_titles and cache and not _is_transcript_summary_query and not _has_transcript_tool_results:
            all_results_str = " ".join(
                json.dumps(r, default=str) for r in cache.values()
            ).lower()

            def _norm(_s: str) -> str:
                _s = (_s or "").lower()
                _s = _s.replace("---", " ").replace("--", " ")
                _s = _s.replace("—", " ").replace("–", " ")
                _s = re.sub(r"[^a-z0-9\s]", " ", _s)
                return re.sub(r"\s+", " ", _s).strip()

            all_results_norm = _norm(all_results_str)
            missing = []
            for title in quoted_titles[:5]:  # check first 5
                tnorm = _norm(title)
                if tnorm and tnorm not in all_results_norm:
                    missing.append(title)
            if missing and len(missing) > len(quoted_titles) // 2:
                issues.append(
                    f"Warning: Some titles ({', '.join(missing[:3])}) were not found in tool results."
                )

        result = ValidationResult(
            status="FAIL" if issues else "PASS",
            issues=issues,
        )

        if result.passed:
            logger.info("Post-hoc validation: PASS")
        else:
            logger.warning("Post-hoc validation: %s", result.summary())

        return result

    # ------------------------------------------------------------------
    # Context budget management
    # ------------------------------------------------------------------

    @staticmethod
    def _compress_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Compress conversation history to fit within context budget.

        Strategy:
        - Always keep system prompt (index 0) and latest user message
        - Summarize older tool results to just their key counts/status
        - Keep the most recent tool result in full
        """
        if len(messages) <= 3:
            return messages  # nothing to compress

        compressed = [messages[0]]  # keep system prompt

        # Find the last tool/user message pair (most recent context)
        # Compress everything in between
        for i, msg in enumerate(messages[1:], 1):
            role = msg.get("role", "")
            content = msg.get("content", "")

            if i >= len(messages) - 3:
                # Keep the last 3 messages intact (recent context)
                compressed.append(msg)
                continue

            if role == "tool" and len(content) > 500:
                # Summarize old tool results
                try:
                    # Try to extract just the count
                    data = json.loads(content.split("] ", 1)[-1] if "] " in content else content)
                    if isinstance(data, dict):
                        items = data.get("data", data)
                        if isinstance(items, list):
                            summary = f"[Previous tool result: {len(items)} items returned]"
                        elif isinstance(items, dict):
                            for k in ("results", "scheduled", "recordings"):
                                if isinstance(items.get(k), list):
                                    summary = f"[Previous tool result: {len(items[k])} {k}]"
                                    break
                            else:
                                summary = f"[Previous tool result: {list(items.keys())[:5]}]"
                        else:
                            summary = content[:200] + "... (compressed)"
                    else:
                        summary = content[:200] + "... (compressed)"
                except (json.JSONDecodeError, Exception):
                    summary = content[:200] + "... (compressed)"

                compressed.append({**msg, "content": summary})
            elif role == "assistant" and len(content) > 300 and i < len(messages) - 3:
                # Compress old assistant reasoning
                compressed.append({**msg, "content": content[:200] + "..."})
            else:
                compressed.append(msg)

        return compressed

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    # Tools requiring explicit user confirmation before execution
    _DANGEROUS_TOOLS = {
        "linux_reboot_server", "linux_shutdown_server",
        "sagetv_delete_media_file", "channels_delete_recording",
        "channels_delete_recording_file",
    }
    _OWNER_TOOLS = {
        "linux_restart_service", "linux_restart_nginx",
        "linux_docker_restart", "sagetv_set_config_value",
        "channels_clear_cache", "channels_rebuild_index",
        "sagetv_run_library_scan", "transcript_reindex",
    }

    def _validate_tool_call(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any] | None:
        """Validate a tool call against cached schemas (IFN/IAN/IAT patterns).

        Returns None if valid, or an error dict if invalid.
        """
        schema = self._tool_schemas.get(tool_name)
        if not schema:
            # IFN: tool name not found in any schema
            # Check for close matches to suggest
            prefixes = ("sagetv_", "channels_", "linux_", "transcript_")
            prefix = ""
            for p in prefixes:
                if tool_name.startswith(p):
                    prefix = p
                    break
            suggestions = [n for n in self._tool_schemas if n.startswith(prefix)][:5]
            return {
                "error": f"Unknown tool '{tool_name}'. "
                f"Did you mean one of: {', '.join(suggestions)}?" if suggestions
                else f"Unknown tool '{tool_name}'."
            }

        props = schema.get("properties", {})
        required = set(schema.get("required", []))

        # IAN: drop unknown parameter names with a warning instead of erroring.
        # Hard-failing causes 7B models to abandon the task entirely; silently
        # discarding extras lets the call succeed with the valid params.
        unknown_params = set(args.keys()) - set(props.keys()) - {"_confirmed"}
        if unknown_params:
            logger.warning(
                "Stripping unknown parameter(s) %s from %s call (valid: %s)",
                list(unknown_params), tool_name, list(props.keys()),
            )
            for k in unknown_params:
                args.pop(k, None)

        # Check required parameters are present
        missing = required - set(args.keys())
        if missing:
            return {
                "error": f"Missing required parameter(s) {list(missing)} for {tool_name}."
            }

        # IAT: type coercion — fix common type mismatches instead of rejecting
        for pname, pdef in props.items():
            if pname not in args:
                continue
            expected_type = pdef.get("type", "")
            val = args[pname]
            try:
                if expected_type == "integer" and isinstance(val, str):
                    args[pname] = int(val)
                elif expected_type == "number" and isinstance(val, str):
                    args[pname] = float(val)
                elif expected_type == "boolean" and isinstance(val, str):
                    args[pname] = val.lower() in ("true", "1", "yes")
                elif expected_type == "string" and not isinstance(val, str):
                    args[pname] = str(val)
            except (ValueError, TypeError):
                return {
                    "error": f"Parameter '{pname}' for {tool_name} must be {expected_type}, "
                    f"got {type(val).__name__}: {val!r}"
                }

        # Fix wrong-year hallucination in date parameters
        import datetime as _dt_mod
        current_year = str(_dt_mod.date.today().year)
        _DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
        for pname in ("date", "start_date", "end_date", "date_from", "date_to"):
            val = args.get(pname)
            if not isinstance(val, str):
                continue
            m = _DATE_RE.match(val)
            if m and m.group(1) != current_year:
                fixed = f"{current_year}-{m.group(2)}-{m.group(3)}"
                logger.warning("Year fix: %s %s %s -> %s", tool_name, pname, val, fixed)
                args[pname] = fixed

        # Override LLM dates with orchestrator-resolved dates when available.
        # The orchestrator resolves "Sunday" → "(2026-05-03)" and puts it in
        # the query, but the 8b model still gets the date wrong sometimes.
        resolved_start = getattr(self, "_resolved_start", None)
        resolved_end = getattr(self, "_resolved_end", None)
        if resolved_start:
            _date_params = {p for p in ("date", "start_date", "end_date") if p in args}
            if _date_params:
                if resolved_end:
                    # Range query: override start_date + end_date
                    if "start_date" in args and args["start_date"] != resolved_start:
                        logger.warning("Date override: %s start_date %s -> %s",
                                       tool_name, args["start_date"], resolved_start)
                        args["start_date"] = resolved_start
                    if "end_date" in args and args["end_date"] != resolved_end:
                        logger.warning("Date override: %s end_date %s -> %s",
                                       tool_name, args["end_date"], resolved_end)
                        args["end_date"] = resolved_end
                else:
                    # Single-day query: override date or start_date/end_date
                    if "date" in args and args["date"] != resolved_start:
                        logger.warning("Date override: %s date %s -> %s",
                                       tool_name, args["date"], resolved_start)
                        args["date"] = resolved_start
                    if "start_date" in args and args["start_date"] != resolved_start:
                        logger.warning("Date override: %s start_date %s -> %s",
                                       tool_name, args["start_date"], resolved_start)
                        args["start_date"] = resolved_start
                    if "end_date" in args:
                        # For single-day, end_date should be same day
                        end_val = resolved_start.replace(
                            resolved_start[-2:],
                            str(int(resolved_start[-2:]) + 1).zfill(2)
                        ) if "end_date" in args else resolved_start
                        # Actually just use the same date — the tool handles
                        # end_date as inclusive
                        if args["end_date"] != resolved_start:
                            logger.warning("Date override: %s end_date %s -> %s",
                                           tool_name, args["end_date"], resolved_start)
                            args["end_date"] = resolved_start

        return None  # valid

    async def _execute_tool(
        self, tool_name: str, args: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Route a tool call to the correct MCP client."""
        try:
            # Enforce system scope — reject tools for disabled systems
            active = getattr(self, "_active_systems", {"sagetv", "channelsdvr"})
            if tool_name.startswith("sagetv_") and "sagetv" not in active:
                return {"error": "SageTV is not active. Only Channels DVR tools are available."}
            if tool_name.startswith("channels_") and "channelsdvr" not in active:
                return {"error": "Channels DVR is not active. Only SageTV tools are available."}

            # Schema validation (IFN/IAN/IAT)
            validation_error = self._validate_tool_call(tool_name, args)
            if validation_error:
                logger.warning("Schema validation failed for %s: %s", tool_name, validation_error["error"])
                return validation_error

            # Confirmation gate for destructive/owner tools
            if tool_name in self._DANGEROUS_TOOLS or tool_name in self._OWNER_TOOLS:
                if not args.pop("_confirmed", False):
                    risk = "DANGEROUS" if tool_name in self._DANGEROUS_TOOLS else "requires authorization"
                    return {
                        "requires_confirmation": True,
                        "tool": tool_name,
                        "args": args,
                        "message": f"This action ({tool_name}) is {risk} and requires your confirmation."
                    }

            # Enforce temporal guardrail — reject wrong-direction tools
            temporal = getattr(self, "_temporal", "")
            _FUTURE_TOOLS = {"get_upcoming_recordings", "get_upcoming", "schedule_recording"}
            _PAST_TOOLS = {"search_recordings", "get_recordings", "delete_recording"}
            _PRESENT_TOOLS = {"get_now_playing", "get_active_recordings"}
            suffix = tool_name.split("_", 1)[1] if "_" in tool_name else ""
            if temporal == "past" and suffix in _FUTURE_TOOLS:
                logger.warning("Temporal guardrail: rejecting future tool %s for past query", tool_name)
                return {"error": f"Wrong tool: '{tool_name}' is for future/upcoming content. Use search_recordings with start_date/end_date for past queries."}
            if temporal == "past" and suffix in _PRESENT_TOOLS:
                logger.warning("Temporal guardrail: rejecting present tool %s for past query", tool_name)
                return {"error": f"Wrong tool: '{tool_name}' is for live/current content. Use search_recordings with start_date/end_date for past queries."}
            if temporal == "future" and suffix in _PAST_TOOLS:
                logger.warning("Temporal guardrail: rejecting past tool %s for future query", tool_name)
                return {"error": f"Wrong tool: '{tool_name}' is for past recordings. Use get_upcoming_recordings for future/scheduled content."}

            if tool_name.startswith("sagetv_"):
                result = await self._orch._sagetv.call_tool(tool_name, args)
                if tool_name in {"sagetv_search_recordings", "sagetv_search_shows"}:
                    result = await self._retry_search_with_variants(
                        tool_name=tool_name,
                        args=args,
                        result=result,
                        client=self._orch._sagetv,
                    )
                return result
            elif tool_name.startswith("channels_"):
                result = await self._orch._channels.call_tool(tool_name, args)
                if tool_name in {"channels_search_recordings", "channels_search_epg"}:
                    result = await self._retry_search_with_variants(
                        tool_name=tool_name,
                        args=args,
                        result=result,
                        client=self._orch._channels,
                    )
                return result
            elif tool_name.startswith("linux_"):
                return await self._orch._linux.call_tool(tool_name, args)
            elif tool_name.startswith("transcript_"):
                return await self._call_transcription(tool_name, args)
            else:
                return {"error": f"Unknown tool: {tool_name}"}
        except ConnectionError as exc:
            logger.warning("Tool %s connection error: %s", tool_name, exc)
            return {"error": f"Service unavailable: {exc}"}
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            return {"error": str(exc)}

    @staticmethod
    def _query_variants(raw: str) -> list[str]:
        """Generate tolerant search variants for title-like lookups."""
        if not isinstance(raw, str):
            return []
        s = raw.strip()
        if not s:
            return []

        variants: list[str] = []

        # Normalize punctuation/dashes while preserving words.
        norm = s.lower()
        norm = norm.replace("---", " ").replace("--", " ")
        norm = norm.replace("—", " ").replace("–", " ")
        norm = re.sub(r"[^a-z0-9\s]", " ", norm)
        norm = re.sub(r"\s+", " ", norm).strip()
        if norm and norm != s.lower():
            variants.append(norm)

        # Many prompts pass "Show --- Episode"; try show-only and episode-only.
        split_dash = re.split(r"\s*(?:---|--|—|–|-)\s*", s)
        if len(split_dash) >= 2:
            show_only = split_dash[0].strip()
            tail_only = split_dash[-1].strip()
            if show_only:
                variants.append(show_only)
            if tail_only:
                variants.append(tail_only)

        # De-duplicate while preserving order.
        out: list[str] = []
        seen: set[str] = set()
        for v in variants:
            k = v.lower().strip()
            if k and k not in seen:
                seen.add(k)
                out.append(v)
        return out

    async def _retry_search_with_variants(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        client: Any,
    ) -> Dict[str, Any]:
        """Retry empty title/query search results with normalized query variants."""
        if isinstance(result, dict) and result.get("error"):
            return result

        n_items = _count_items(result)
        if n_items is None or n_items > 0:
            return result

        key = "title" if isinstance(args.get("title"), str) else "query" if isinstance(args.get("query"), str) else None
        if not key:
            return result

        original = str(args.get(key) or "").strip()
        for candidate in self._query_variants(original):
            if candidate.lower() == original.lower():
                continue
            retry_args = dict(args)
            retry_args[key] = candidate
            try:
                retry = await client.call_tool(tool_name, retry_args)
            except Exception:
                continue

            retry_items = _count_items(retry)
            if retry_items is not None and retry_items > 0:
                logger.info(
                    "Search fallback succeeded for %s: %s -> %s (%d items)",
                    tool_name,
                    original,
                    candidate,
                    retry_items,
                )
                return retry

        return result

    async def _call_transcription(
        self, tool_name: str, args: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Call a transcript tool via direct TCP JSON-RPC to port 8770."""
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 8770, limit=1024 * 1024)
            request = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }) + "\n"
            writer.write(request.encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            writer.close()
            await writer.wait_closed()

            if not line:
                return {"error": "Empty response from transcription server"}

            resp = json.loads(line.decode())
            if "error" in resp:
                err = resp["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                return {"error": msg}

            result = resp.get("result", {})
            content = result.get("content", [])
            if content and isinstance(content, list):
                text = content[0].get("text", "{}")
                try:
                    parsed = json.loads(text)

                    # transcript_cross_search is transcript-text FTS and
                    # transcript_search can miss punctuation-heavy title queries
                    # like "Show --- Episode". If empty, use a fuzzy title
                    # fallback against recent transcript metadata.
                    if tool_name in {"transcript_cross_search", "transcript_search"}:
                        query = str((args or {}).get("query") or "").strip()
                        if query:
                            data = parsed.get("data", parsed) if isinstance(parsed, dict) else {}
                            rows = []
                            if isinstance(data, dict):
                                for key in ("results", "recent", "recordings", "items"):
                                    maybe_rows = data.get(key)
                                    if isinstance(maybe_rows, list):
                                        rows = maybe_rows
                                        break
                            if not rows:
                                fallback = await self._call_transcription(
                                    "transcript_list_recent", {"limit": 200}
                                )
                                fdata = fallback.get("data", fallback) if isinstance(fallback, dict) else {}
                                recent = fdata.get("recent", []) if isinstance(fdata, dict) else []

                                if recent:
                                    import difflib as _difflib

                                    def _norm(_s: str) -> str:
                                        _s = (_s or "").lower()
                                        _s = _s.replace("---", " ").replace("--", " ")
                                        _s = re.sub(r"[^a-z0-9]+", " ", _s)
                                        return re.sub(r"\s+", " ", _s).strip()

                                    seeds = [query] + self._query_variants(query)
                                    best = None
                                    best_score = 0.0
                                    for row in recent:
                                        cand = " ".join([
                                            str(row.get("title") or ""),
                                            str(row.get("episode_title") or row.get("episode") or ""),
                                        ]).strip()
                                        cn = _norm(cand)
                                        if not cn:
                                            continue
                                        score = 0.0
                                        for seed in seeds:
                                            qn = _norm(seed)
                                            if not qn:
                                                continue
                                            qtokens = set(qn.split())
                                            ratio = _difflib.SequenceMatcher(None, qn, cn).ratio()
                                            ctokens = set(cn.split())
                                            overlap = len(qtokens & ctokens) / max(1, len(qtokens)) if qtokens else 0.0
                                            local_score = max(ratio, overlap)
                                            if qn in cn:
                                                local_score = max(local_score, 0.95)
                                            score = max(score, local_score)
                                        if score > best_score:
                                            best_score = score
                                            best = row

                                    if best is not None and best_score >= 0.45:
                                        return {
                                            "success": True,
                                            "data": {
                                                "results": [best],
                                                "total": 1,
                                                "query": query,
                                                "mode": f"{tool_name}_title_fuzzy_fallback",
                                            },
                                        }

                    return parsed
                except json.JSONDecodeError:
                    return {"raw": text}
            return result
        except Exception as exc:
            logger.warning("Transcript tool %s failed: %s", tool_name, exc)
            return {"error": str(exc)}
