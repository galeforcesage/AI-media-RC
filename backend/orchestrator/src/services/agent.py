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
from typing import Any, Awaitable, Callable, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

MAX_ITERATIONS = 5  # default; overridden by config.agent.max_iterations

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
transcript_search(query, limit?) | transcript_cross_search(query, actor?, genre?, channel?, date_from?, date_to?, limit?) | transcript_actors(actor_name, limit?) | transcript_stats() | transcript_get(recording_id) | transcript_recording_summary(recording_id) | transcript_jobs(status?) | transcript_reindex(directory?)
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


class AgentLoop:
    """Manages the tool-calling loop between the LLM and MCP servers."""

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator
        self._max_iterations = orchestrator.config.get("agent", {}).get(
            "max_iterations", MAX_ITERATIONS
        )
        self._dynamic_tools: Dict[str, str] | None = None  # cached dynamic tool text
        self._openai_tools: List[Dict[str, Any]] | None = None  # cached OpenAI-format tools

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
                "description": "Cross-metadata transcript search with optional filters for actor, genre, channel, date range.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Full-text search query"},
                        "actor": {"type": "string", "description": "Filter by actor name"},
                        "genre": {"type": "string", "description": "Filter by genre"},
                        "channel": {"type": "string", "description": "Filter by channel"},
                        "date_from": {"type": "string", "description": "Filter from date (ISO)"},
                        "date_to": {"type": "string", "description": "Filter to date (ISO)"},
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
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

    async def _discover_openai_tools(self, systems: list[str] | None = None) -> List[Dict[str, Any]]:
        """Query each MCP server and build OpenAI-format tool schemas.

        Returns a list of {"type": "function", "function": {...}} dicts
        ready to pass as the ``tools`` parameter to Ollama /api/chat.
        Only includes essential query/info tools to fit in context window.
        """
        all_systems = {"sagetv", "channelsdvr"}
        active = set(systems) if systems else all_systems

        tools: List[Dict[str, Any]] = []

        server_map = []
        if "channelsdvr" in active and hasattr(self._orch, "_channels"):
            server_map.append(("channelsdvr", self._orch._channels, "Channels DVR"))
        if "sagetv" in active and hasattr(self._orch, "_sagetv"):
            server_map.append(("sagetv", self._orch._sagetv, "SageTV"))
        if hasattr(self._orch, "_linux"):
            server_map.append(("linux", self._orch._linux, "Linux"))

        for sys_key, client, label in server_map:
            try:
                mcp_tools = await client.list_tools()
                for t in mcp_tools:
                    if t["name"] in self._ESSENTIAL_TOOLS:
                        tools.append(self._mcp_to_openai_tool(t))
                logger.info("Discovered %d OpenAI-format tools from %s (of %d total)",
                            sum(1 for t in mcp_tools if t["name"] in self._ESSENTIAL_TOOLS),
                            label, len(mcp_tools))
            except Exception as exc:
                logger.warning("Could not discover %s tools for OpenAI format: %s", label, exc)

        # Add transcript tools (filtered to essential only)
        for t in self._TRANSCRIPT_TOOLS_OPENAI:
            if t["function"]["name"] in self._ESSENTIAL_TOOLS:
                tools.append(t)
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
        current_time = now.strftime("%I:%M %p %Z")
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

        return (
            f"You are an AI media assistant. Today: {today}, {current_time}.\n"
            f"{scope_line}\n\n"

            "MANDATORY TOOL USE:\n"
            "- You MUST call a tool to answer any question about recordings, schedules, playback, or system status.\n"
            "- You do NOT have access to live DVR data without calling a tool.\n"
            "- If you cannot determine which tool to use, respond with: \"I don't have enough information to answer that.\"\n"
            "- NEVER fabricate or guess DVR data. Only report what tools return.\n"
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
            "- Final answers: plain language, concise. No JSON, no tool names, no code blocks, no IDs.\n"
            "- Never include server file paths or directory paths in answers.\n"
            "- Never describe your reasoning steps. Just give the answer.\n\n"

            "OUTPUT FORMAT:\n"
            "- ALWAYS wrap every show name in double quotes: \"NCIS\", \"Will Trent\"\n"
            "- ALWAYS include the episode title from the tool result and wrap it in double quotes.\n"
            "- Format: \"ShowName\" \"EpisodeTitle\" S##E## — every line MUST have all three parts.\n"
            "- Example: \"NCIS\" \"Toil and Trouble\" S23E19\n"
            "- If the recording has watched=true, append ✓ (watched). Example: \"NCIS\" \"Toil and Trouble\" S23E19 ✓\n"
            "- Do NOT omit episode titles. Do NOT skip them. The user needs them.\n"
            "- Do NOT include descriptions, air times, or channel numbers unless asked.\n\n"

            "PATHS:\n"
            + ("- SageTV: /var/media/tv\n" if has_sagetv else "")
            + (channels_path_line if has_channels else "")
        )

    async def run(
        self,
        user_query: str,
        transcript_context: str = "",
        semantic_context: str = "",
        systems: list[str] | None = None,
        temporal: str = "",
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

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": await self._build_system_prompt(systems)},
        ]
        logger.info("System prompt: %d chars", len(messages[0]["content"]))

        # Store active systems and temporal intent for tool-call guardrails
        self._active_systems = set(systems) if systems else {"sagetv", "channelsdvr"}
        self._temporal = temporal or ""

        # Discover OpenAI-format tool schemas for native tool calling
        openai_tools = await self._discover_openai_tools(systems)
        logger.info("Passing %d tool schemas to Ollama", len(openai_tools))

        # Build user message with pre-fetched context
        context_parts = []
        if semantic_context:
            context_parts.append(
                "Background context from media library (may not match the query date — "
                "always use tools to answer date-specific questions):\n"
                f"{semantic_context}"
            )
        if transcript_context:
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

            # ── Call LLM with native tool schemas ──
            # On tool-call iterations we don't stream tokens to frontend
            # (the model returns structured tool_calls, not text).
            # On final-answer iterations we stream tokens.
            _forwarding = True
            _tool_detected = False
            _buffer: List[str] = []
            _buffer_len = 0
            _BUFFER_THRESHOLD = 20

            async def _on_token(token: str):
                nonlocal _forwarding, _tool_detected, _buffer, _buffer_len
                if _tool_detected:
                    return
                _buffer.append(token)
                _buffer_len += len(token)
                # With native tool calling, the model shouldn't output
                # <tool_call> tags, but check anyway for safety
                buf_text = "".join(_buffer)
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
            )

            # Flush remaining buffered tokens (for non-tool-call responses)
            if _buffer and _forwarding and token_callback:
                for t in _buffer:
                    await token_callback(t)
                _buffer.clear()

            if llm_result.get("error"):
                return llm_result

            response_text = llm_result.get("response", "")
            native_tool_calls = llm_result.get("tool_calls", [])

            logger.info(
                "LLM response (iter %d, %d chars, %d native tool_calls): %s",
                iteration + 1, len(response_text),
                len(native_tool_calls), response_text[:200],
            )

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

            if not tool_calls:
                # No tool calls detected in any format
                if iteration == 0:
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

            for idx, tc in enumerate(tool_calls):
                tool_name = tc.get("tool", "")
                tool_args = tc.get("args") or {}
                logger.info("Executing tool: %s(%s)", tool_name, json.dumps(tool_args, default=str)[:200])

                if status_callback:
                    await status_callback(_tool_status_message(tool_name, tool_args))

                result = await self._execute_tool(tool_name, tool_args)
                result_slim = _slim_for_llm(result)
                result_str = json.dumps(result_slim, default=str)
                logger.debug("Slimmed result for %s (%d chars): %.800s", tool_name, len(result_str), result_str)

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
                    tool_messages.append({
                        "role": "tool",
                        "content": result_str,
                    })
                else:
                    tool_results.append(f"Tool: {tool_name}\nResult: {result_str}")

            # ── Inject tool results back into conversation ──
            if tool_messages:
                # Native path: add each tool result as a role:tool message
                messages.extend(tool_messages)
                if has_service_error:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Some services are currently offline. "
                            "Do NOT retry the same tool. Tell the user which service "
                            "is unavailable and answer with whatever information you have."
                        ),
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
                        "Otherwise, provide your final answer."
                    ),
                })

        # Max iterations reached
        logger.warning("Agent loop reached max iterations (%d)", self._max_iterations)
        raw = llm_result.get("response", "")
        return {
            "status": "ok",
            "response": (
                "I wasn't able to fully resolve your request within the "
                "allowed steps. Here's what I found so far: "
                + self._strip_markers(raw)
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

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

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
                return await self._orch._sagetv.call_tool(tool_name, args)
            elif tool_name.startswith("channels_"):
                return await self._orch._channels.call_tool(tool_name, args)
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
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}
            return result
        except Exception as exc:
            logger.warning("Transcript tool %s failed: %s", tool_name, exc)
            return {"error": str(exc)}
