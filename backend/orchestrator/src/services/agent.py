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
    "description", "duration_min", "original_date", "channel", "start_time",
}


def _slim_for_llm(obj):
    """Recursively strip frontend-only fields from tool results to save LLM tokens."""
    if isinstance(obj, dict):
        return {k: _slim_for_llm(v) for k, v in obj.items() if k not in _LLM_STRIP_FIELDS}
    if isinstance(obj, list):
        return [_slim_for_llm(item) for item in obj]
    return obj

# Tool definitions split by system for filtering based on user's LLM Focus selection.
# Keys: "sagetv", "channelsdvr", "shared" (linux + transcript — always included)
# COMPACT format: tool(required_param, optional_param?) — description
_TOOL_SECTIONS = {
    "sagetv": """
## SageTV Tools
Query: sagetv_search_recordings(title?, channel?, start_date? YYYY-MM-DD, end_date? YYYY-MM-DD, limit?) | sagetv_get_recordings(limit?, offset?) | sagetv_get_recent_recordings(limit?) | sagetv_get_active_recordings() | sagetv_get_upcoming_recordings() | sagetv_get_now_playing() | sagetv_search_shows(query) | sagetv_get_recording(media_file_id) | sagetv_get_airing(airing_id) | sagetv_get_show(show_id)
System: sagetv_get_channels() | sagetv_get_channel(station_id) | sagetv_get_disk_space() | sagetv_get_tuner_status() | sagetv_get_clients()
Playback: sagetv_pause_playback() | sagetv_resume_playback() | sagetv_stop_playback() | sagetv_skip_forward() | sagetv_skip_back() | sagetv_seek_relative(seconds) | sagetv_seek_absolute(position_seconds) | sagetv_set_volume(level) | sagetv_mute() | sagetv_unmute() | sagetv_commercial_skip() | sagetv_tune_channel(channel)
Nav: sagetv_open_recordings() | sagetv_open_guide() | sagetv_open_home() | sagetv_open_live_tv()
Manage: sagetv_record_show(airing_id) | sagetv_cancel_recording(airing_id) | sagetv_delete_media_file(media_file_id) | sagetv_set_watched(airing_id, watched?) | sagetv_set_archived(media_file_id, archived?)
Favorites: sagetv_create_favorite(title, channel?) | sagetv_remove_favorite(favorite_id)
Config: sagetv_get_config_value(key) | sagetv_set_config_value(key, value) | sagetv_run_library_scan()
""",

    "channelsdvr": """
## Channels DVR Tools
Search recordings: channels_search_recordings(title?, channel?, start_date? YYYY-MM-DD, end_date? YYYY-MM-DD, limit?) — USE THIS to find what was recorded on a date
List recordings: channels_get_recordings(limit?) — list all recordings
Upcoming: channels_get_upcoming_recordings(date? YYYY-MM-DD, start_date? YYYY-MM-DD, end_date? YYYY-MM-DD) — list upcoming episodes scheduled to record. Use date for a single day (defaults to today), or start_date+end_date for a range (e.g. this week). USE THIS for "what's recording today/tonight/this week"
Live: channels_get_now_playing() — what is airing live right now
EPG: channels_search_epg(query) — search the electronic program guide for upcoming shows
Info: channels_get_channels() | channels_get_storage_status() | channels_get_jobs() | channels_get_clients()
Passes: channels_get_scheduled_recordings() — lists recording RULES/passes, NOT actual recordings
Playback: channels_get_bridge_devices() | channels_get_playback_status(device?) | channels_pause_playback(device?) | channels_resume_playback(device?) | channels_toggle_pause(device?) | channels_stop_playback(device?) | channels_skip_commercial(device?) | channels_seek_relative(seconds, device?) | channels_seek_forward(device?) | channels_seek_backward(device?) | channels_toggle_mute(device?) | channels_toggle_cc(device?) | channels_play_channel(channel_number, device?) | channels_play_recording(recording_id, device?) | channels_channel_up(device?) | channels_channel_down(device?)
Manage: channels_schedule_recording(program_id, channel) | channels_schedule_series_recording(series_id, channel?) | channels_cancel_scheduled_recording(id) | channels_delete_recording(id) | channels_delete_recording_file(id) | channels_regenerate_commercial_markers(id)
System: channels_clear_cache() | channels_rebuild_index()
""",

    "shared": """
## Linux Tools
Info: linux_disk_usage() | linux_memory_info() | linux_uptime() | linux_network_info()
Files: linux_list_directory(path) | linux_file_info(path) | linux_count_files(root, pattern) | linux_find_large_files(root, sort_by?, extension?)
Services: linux_service_status(service_name) | linux_restart_service(service_name) | linux_docker_ps() | linux_docker_restart(container) | linux_docker_logs(container, lines?) | linux_tail_log(path, lines?)
Danger: linux_reboot_server() | linux_shutdown_server() | linux_restart_nginx()

## Transcript Tools
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
    "channels_search_recordings": "USE THIS to find what was recorded on a date",
    "channels_get_upcoming_recordings": "USE THIS for 'what's recording today/tonight/this week'",
    "channels_get_scheduled_recordings": "lists recording RULES/passes, NOT actual recordings",
    "channels_get_now_playing": "what is airing live right now",
    "channels_search_epg": "search program guide for upcoming shows",
    "sagetv_search_recordings": "search recordings by title/date",
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
    "channels_get_jobs": "Checking DVR jobs",
    "channels_get_clients": "Getting connected clients",
    "sagetv_search_recordings": "Searching SageTV recordings",
    "sagetv_get_recordings": "Fetching SageTV recordings",
    "sagetv_get_recent_recordings": "Getting recent recordings",
    "sagetv_get_active_recordings": "Checking active recordings",
    "sagetv_get_upcoming_recordings": "Getting upcoming recordings",
    "sagetv_get_now_playing": "Checking what's playing now",
    "sagetv_search_shows": "Searching SageTV shows",
    "transcript_search": "Searching transcripts",
    "transcript_cross_search": "Cross-searching transcripts",
    "transcript_stats": "Getting transcript stats",
    "linux_disk_usage": "Checking disk usage",
    "linux_memory_info": "Checking memory",
    "linux_uptime": "Checking system uptime",
    "linux_service_status": "Checking service status",
    "linux_docker_ps": "Listing containers",
}


def _tool_status_message(tool_name: str) -> str:
    """Return a human-readable status message for a tool call."""
    if tool_name in _TOOL_STATUS:
        return _TOOL_STATUS[tool_name]
    # Fallback: derive from tool name
    for prefix, system in [("channels_", "Channels DVR"), ("sagetv_", "SageTV"),
                           ("linux_", "system"), ("transcript_", "transcripts")]:
        if tool_name.startswith(prefix):
            action = tool_name[len(prefix):].replace("_", " ")
            if "play" in action or "pause" in action or "stop" in action or "seek" in action:
                return f"Controlling {system} playback"
            return f"Querying {system}"
    return f"Running {tool_name}"


class AgentLoop:
    """Manages the tool-calling loop between the LLM and MCP servers."""

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator
        self._max_iterations = orchestrator.config.get("agent", {}).get(
            "max_iterations", MAX_ITERATIONS
        )
        self._dynamic_tools: Dict[str, str] | None = None  # cached dynamic tool text

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

            "TOOL CALL FORMAT — respond with ONLY this when calling a tool:\n"
            "<tool_call>\n"
            '{"tool": "tool_name", "args": {"param": "value"}}\n'
            "</tool_call>\n\n"

            "RULES:\n"
            "- For date-based queries (today, yesterday, last week), ALWAYS call the DVR search tool with start_date/end_date. Do not answer from context alone.\n"
            "- For 'what is recording today', 'what's scheduled', 'upcoming recordings', ALWAYS call sagetv_get_upcoming_recordings() and/or channels_get_upcoming_recordings(). Never answer from memory.\n"
            "- Use DVR tools for recordings, playback, EPG. Use linux_ tools only for filesystem/services.\n"
            "- For date searches, use start_date/end_date as YYYY-MM-DD in "
            + "/".join(search_tools) + ".\n"
            "- Never delete DVR files via linux_ tools. Use the DVR's delete tool.\n"
            "- Destructive tools require user confirmation first.\n"
            "- On tool error, tell the user briefly. Do NOT retry.\n"
            "- Final answers: plain language, concise. No JSON, no tool names, no code blocks, no IDs.\n"
            "- Never include server file paths or directory paths in answers.\n"
            "- Never describe your reasoning steps. Just give the answer.\n"
            "- Only use tools listed below.\n\n"

            "OUTPUT FORMAT:\n"
            "- ALWAYS wrap every show name in double quotes: \"NCIS\", \"Will Trent\"\n"
            "- ALWAYS include the episode title from the tool result and wrap it in double quotes.\n"
            "- Format: \"ShowName\" \"EpisodeTitle\" S##E## — every line MUST have all three parts.\n"
            "- Example: \"NCIS\" \"Toil and Trouble\" S23E19\n"
            "- Do NOT omit episode titles. Do NOT skip them. The user needs them.\n"
            "- Do NOT include descriptions, air times, or channel numbers unless asked.\n\n"

            "PATHS:\n"
            + ("- SageTV: /var/media/tv\n" if has_sagetv else "")
            + (channels_path_line if has_channels else "")
            + "\n"
            + await self._discover_tools(systems)
        )

    async def run(
        self,
        user_query: str,
        transcript_context: str = "",
        semantic_context: str = "",
        systems: list[str] | None = None,
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
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": await self._build_system_prompt(systems)},
        ]
        logger.info("System prompt: %d chars", len(messages[0]["content"]))

        # Store active systems for tool-call guardrail
        self._active_systems = set(systems) if systems else {"sagetv", "channelsdvr"}

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

            # Stream tokens from the LLM. We forward tokens to the frontend
            # until we detect a <tool_call> tag, then stop forwarding.
            _forwarding = True
            _tool_detected = False

            async def _on_token(token: str):
                nonlocal _forwarding, _tool_detected
                if _tool_detected:
                    return
                # Check accumulated text so far for tool_call marker
                if "<tool_call>" in token or "<tool" in token:
                    _tool_detected = True
                    _forwarding = False
                    # Tell frontend to clear partial tokens (tool call coming)
                    if status_callback:
                        await status_callback("Calling tools")
                    return
                if _forwarding and token_callback:
                    await token_callback(token)

            llm_result = await self._orch.llm.stream_chat(
                messages, token_callback=_on_token
            )
            if llm_result.get("error"):
                return llm_result

            response_text = llm_result.get("response", "")
            logger.info(
                "LLM response (iter %d, %d chars): %s",
                iteration + 1, len(response_text), response_text[:200],
            )

            tool_calls = self._parse_tool_calls(response_text)

            if not tool_calls:
                # Guard: if iter 0 and response mentions a tool name without
                # actually calling it, nudge the LLM to use <tool_call> format.
                if iteration == 0 and any(
                    t in response_text for t in (
                        "channels_get_upcoming", "sagetv_get_upcoming",
                        "channels_search_recordings", "sagetv_search_recordings",
                        "I will use", "let me check", "let me use",
                    )
                ):
                    logger.info("LLM mentioned tool without calling — nudging retry")
                    messages.append({"role": "assistant", "content": response_text})
                    messages.append({
                        "role": "user",
                        "content": (
                            "You mentioned a tool but did not call it. "
                            "You MUST use <tool_call> tags. Try again."
                        ),
                    })
                    continue

                final = self._strip_markers(response_text)
                return {
                    "status": "ok",
                    "response": final,
                    "model": llm_result.get("model", ""),
                    "iterations": iteration + 1,
                    "streamed": True,
                }

            # Append assistant message, execute tools, feed back results
            messages.append({"role": "assistant", "content": response_text})

            tool_results: List[str] = []
            has_service_error = False
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                tool_args = tc.get("args") or {}
                logger.info("Executing tool: %s(%s)", tool_name, json.dumps(tool_args, default=str)[:200])

                if status_callback:
                    await status_callback(_tool_status_message(tool_name))

                result = await self._execute_tool(tool_name, tool_args)
                result_slim = _slim_for_llm(result)
                result_str = json.dumps(result_slim, default=str)
                logger.debug("Slimmed result for %s (%d chars): %.800s", tool_name, len(result_str), result_str)
                if len(result_str) > 4000:
                    result_str = result_str[:4000] + "... (truncated)"
                tool_results.append(f"Tool: {tool_name}\nResult: {result_str}")

                if result.get("error") and "unavailable" in str(result["error"]).lower():
                    has_service_error = True

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
        return {
            "status": "ok",
            "response": (
                "I wasn't able to fully resolve your request within the "
                "allowed steps. Here's what I found so far: "
                + llm_result.get("response", "")
            ),
            "model": llm_result.get("model", ""),
            "iterations": self._max_iterations,
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    # Pattern 1: properly closed <tool_call>...</tool_call>
    _TOOL_CALL_RE = re.compile(
        r"<tool_call>\s*(\{.*\})\s*</tool_call>", re.DOTALL,
    )
    # Pattern 2: unclosed <tool_call> followed by JSON (common with mistral)
    _TOOL_CALL_OPEN_RE = re.compile(
        r"<tool_call>\s*(\{[^<]+\})", re.DOTALL,
    )

    def _parse_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        """Extract tool call JSON blocks from LLM response text.

        Handles: <tool_call>{...}</tool_call>, <tool_call>{...} (unclosed),
        and multiple tool calls in one response.
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
                if "tool" in parsed:
                    if "args" not in parsed:
                        parsed["args"] = {}
                    calls.append(parsed)
                else:
                    logger.warning("Tool call missing 'tool' key: %s", json_str[:200])
            except json.JSONDecodeError:
                logger.warning("Malformed tool call JSON: %s", json_str[:200])
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
        text = re.sub(r"</?tool_call>", "", text)
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
