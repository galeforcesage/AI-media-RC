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
from typing import Any, Dict, List

from utils.logger import get_logger

logger = get_logger(__name__)

MAX_ITERATIONS = 5

TOOL_DEFINITIONS = """
## Available Tools

### SageTV DVR — Query & Metadata
- **sagetv_get_now_playing**: What's currently playing on SageTV. Params: session_id (optional).
- **sagetv_get_recordings**: List recordings with paging. Params: limit (int, optional), offset (int, optional).
- **sagetv_search_recordings**: Search recordings by filters. Params: title (str, optional), channel (str, optional), start_time (str, optional), end_time (str, optional), watched (bool, optional), archived (bool, optional), recording_state (str, optional), limit (int, optional).
- **sagetv_get_recent_recordings**: Most recently completed recordings. Params: limit (int, optional).
- **sagetv_get_active_recordings**: Recordings currently in progress. No params.
- **sagetv_get_upcoming_recordings**: Scheduled future recordings. No params.
- **sagetv_search_shows**: Search the program guide by title. Params: query (str, required).
- **sagetv_get_channels**: List all channels. No params.
- **sagetv_get_channel**: Get channel info by station ID. Params: station_id (str, required).
- **sagetv_get_disk_space**: Disk space info. No params.
- **sagetv_get_tuner_status**: Tuner status. No params.
- **sagetv_get_clients**: List connected SageTV clients. No params.
- **sagetv_get_recording**: Get single recording (fully hydrated). Params: media_file_id (str, required).
- **sagetv_get_airing**: Get airing by ID. Params: airing_id (str, required).
- **sagetv_get_show**: Get show metadata. Params: show_id (str, required).

### SageTV DVR — Playback Control
- **sagetv_pause_playback**: Pause playback. Params: session_id (optional).
- **sagetv_resume_playback**: Resume playback. Params: session_id (optional).
- **sagetv_stop_playback**: Stop playback. Params: session_id (optional).
- **sagetv_skip_forward**: Skip forward. Params: session_id (optional).
- **sagetv_skip_back**: Skip backward. Params: session_id (optional).
- **sagetv_seek_relative**: Seek by seconds. Params: session_id (optional), seconds (int, required).
- **sagetv_seek_absolute**: Seek to absolute position. Params: session_id (optional), position_seconds (int, required).
- **sagetv_set_volume**: Set volume 0-100. Params: session_id (optional), level (int, required).
- **sagetv_mute**: Mute audio. Params: session_id (optional).
- **sagetv_unmute**: Unmute audio. Params: session_id (optional).
- **sagetv_commercial_skip**: Skip current commercial break. Params: session_id (optional).
- **sagetv_get_commercial_segments**: Get commercial break segments. Params: media_file_id (str, required).
- **sagetv_tune_channel**: Tune to a channel. Params: session_id (optional), channel (str, required).

### SageTV DVR — Navigation
- **sagetv_open_recordings**: Navigate to recordings screen. Params: session_id (optional).
- **sagetv_open_guide**: Navigate to program guide. Params: session_id (optional).
- **sagetv_open_home**: Navigate to home screen. Params: session_id (optional).
- **sagetv_open_live_tv**: Navigate to live TV. Params: session_id (optional).

### SageTV DVR — Recording Management (CONFIRM required)
- **sagetv_record_show**: Schedule a recording. Params: airing_id (str, required).
- **sagetv_cancel_recording**: Cancel scheduled recording. Params: airing_id (str, required).
- **sagetv_delete_media_file**: Permanently delete recorded file. Params: media_file_id (str, required).
- **sagetv_set_watched**: Mark watched/unwatched. Params: airing_id (str, required), watched (bool, optional, default true).
- **sagetv_set_archived**: Archive/protect from auto-delete. Params: media_file_id (str, required), archived (bool, optional, default true).

### SageTV DVR — Favorites
- **sagetv_create_favorite**: Create series recording favorite. Params: title (str, required), channel (str, optional).
- **sagetv_remove_favorite**: Remove series favorite. Params: favorite_id (str, required).

### SageTV DVR — Configuration & System
- **sagetv_get_config_value**: Get SageTV config property. Params: key (str, required).
- **sagetv_set_config_value**: Set SageTV config property. Params: key (str, required), value (str, required).
- **sagetv_run_library_scan**: Trigger library rescan. No params.
- **sagetv_get_media_file_property**: Get custom metadata. Params: media_file_id (str, required), key (str, required).
- **sagetv_set_media_file_property**: Set custom metadata. Params: media_file_id (str, required), key (str, required), value (str, required).

### Channels DVR — Query & Metadata
- **channels_get_now_playing**: Active playback sessions on Channels DVR. No params.
- **channels_get_recordings**: List DVR recordings. Params: limit (int, optional).
- **channels_search_epg**: Search the EPG. Params: query (str, required).
- **channels_get_channels**: List all channels. No params.
- **channels_get_storage_status**: DVR storage info including recording directory path. No params.
- **channels_get_scheduled_recordings**: Get all recording rules. No params.
- **channels_get_jobs**: Get all DVR jobs (recording, comskip, transcode). No params.
- **channels_get_clients**: List connected Channels DVR clients. No params.

### Channels DVR — Playback Control
- **channels_pause_playback**: Pause. Params: session_id (str, required).
- **channels_resume_playback**: Resume. Params: session_id (str, required).
- **channels_stop_playback**: Stop. Params: session_id (str, required).
- **channels_skip_commercial**: Skip commercial. Params: session_id (str, required).
- **channels_seek_relative**: Seek forward/back. Params: session_id (str, required), seconds (int, required).
- **channels_seek_absolute**: Seek to position. Params: session_id (str, required), position_seconds (int, required).
- **channels_previous_commercial**: Jump to previous commercial marker. Params: session_id (str, required).
- **channels_set_playback_speed**: Set playback speed. Params: session_id (str, required), rate (float, required).

### Channels DVR — Recording Management (CONFIRM required)
- **channels_schedule_recording**: Schedule one-time recording. Params: program_id (str, required), channel (str, required), start_time (str, optional), end_time (str, optional).
- **channels_schedule_series_recording**: Schedule series pass. Params: series_id (str, required), channel (str, optional), options (dict, optional).
- **channels_cancel_scheduled_recording**: Cancel recording rule. Params: id (str, required).
- **channels_delete_recording**: Mark recording for removal. Params: id (str, required).
- **channels_delete_recording_file**: Permanently delete recording file. Params: id (str, required).
- **channels_regenerate_commercial_markers**: Regenerate commercial markers. Params: id (str, required).

### Channels DVR — System
- **channels_clear_cache**: Clear Channels DVR cache. No params.
- **channels_rebuild_index**: Rebuild media index. No params.

### Linux System — Info
- **linux_disk_usage**: Disk usage for all mounts. No params.
- **linux_memory_info**: RAM usage. No params.
- **linux_uptime**: Uptime and load averages. No params.
- **linux_network_info**: Network interfaces. No params.

### Linux System — File Operations
- **linux_count_files**: Count files matching a glob. Params: root (str, required), pattern (str, required).
- **linux_list_directory**: List files in a directory. Params: path (str, required).
- **linux_file_info**: Get file/directory metadata. Params: path (str, required).
- **linux_find_large_files**: Find large files. Params: root (str, required), sort_by (str, optional: "size"/"age"), extension (str, optional).

### Linux System — Services & Docker
- **linux_docker_ps**: List running Docker containers. No params.
- **linux_service_status**: Get status of a system service. Params: service_name (str, required).
- **linux_restart_service**: Restart a system service. Params: service_name (str, required).
- **linux_docker_restart**: Restart a Docker container. Params: container (str, required).
- **linux_docker_logs**: View container logs. Params: container (str, required), lines (int, optional).
- **linux_tail_log**: View last N lines of a log file. Params: path (str, required), lines (int, optional, max 500).

### Linux System — Server Control (DANGEROUS)
- **linux_reboot_server**: Reboot the Linux server. No params.
- **linux_shutdown_server**: Shut down the Linux server. No params.
- **linux_restart_nginx**: Restart nginx. No params.

### Transcript Search
- **transcript_search**: Full-text search across all transcripts. Params: query (str, required), limit (int, optional).
- **transcript_cross_search**: Search with metadata filters. Params: query (str, required), actor (str, optional), genre (str, optional), channel (str, optional), date_from (str, optional), date_to (str, optional), limit (int, optional).
- **transcript_actors**: Find recordings featuring an actor. Params: actor_name (str, required), limit (int, optional).
- **transcript_stats**: Transcription statistics. No params.
- **transcript_get**: Get transcript for a recording. Params: recording_id (str, required).
- **transcript_recording_summary**: Get enriched summary (metadata+actors+transcript). Params: recording_id (str, required).
- **transcript_jobs**: List transcription job queue. Params: status (str, optional: pending/processing/done/error).
- **transcript_reindex**: Reindex transcript sidecar files. Params: directory (str, optional).
"""


class AgentLoop:
    """Manages the tool-calling loop between the LLM and MCP servers."""

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator

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

    async def _build_system_prompt(self) -> str:
        """Build the system prompt with dynamically discovered paths and unified routing rules."""
        import datetime
        now = datetime.datetime.now().astimezone()
        today = now.strftime("%A, %B %d, %Y")
        current_time = now.strftime("%I:%M %p %Z")
        channels_path_line = await self._discover_system_paths()
        # Use string concatenation to avoid .format() conflicts with JSON braces in the prompt
        return (
            "You are the AI Media Remote Control assistant.\n"
            f"Today is {today}. Current time is {current_time}.\n"
            "You control four MCP servers:\n"
            "1. SageTV MCP (DVR engine) — tools prefixed sagetv_\n"
            "2. ChannelsDVR MCP (DVR engine) — tools prefixed channels_\n"
            "3. Linux MCP (filesystem/system helper) — tools prefixed linux_\n"
            "4. Transcript Index (semantic search) — tools prefixed transcript_\n\n"

            "SageTV and ChannelsDVR are functionally identical DVRs targeting different backends. "
            "All DVR logic applies equally to both.\n\n"

            "To use a tool, respond with ONLY a tool call in this exact format:\n\n"
            "<tool_call>\n"
            '{"tool": "tool_name", "args": {"param": "value"}}\n'
            "</tool_call>\n\n"

            # ---- MCP SERVER SELECTION (ROUTING) ----
            "MCP SERVER SELECTION RULES:\n\n"

            "USE A DVR MCP (sagetv_ or channels_) FOR:\n"
            "- Recording metadata, search, deletion, playback\n"
            "- Resume, seek, skip, commercial skip\n"
            "- Airings, EPG, favorites, passes, rules\n"
            "- Retrieving recording file paths\n"
            "- DVR-level rescan/reindex\n"
            "- Any action involving a show, episode, or recording\n\n"

            "USE LINUX MCP ONLY FOR:\n"
            "- Directory listing, disk usage, file size, file existence\n"
            "- Reading raw files, counting files by pattern\n"
            "- Service/container status, log viewing\n"
            "- Moving/deleting ONLY orphaned files (never DVR-managed files)\n"
            "- Validating file paths returned by DVR MCPs\n\n"

            "USE TRANSCRIPT INDEX FOR:\n"
            "- Semantic search: quotes, scenes, characters, dialogue\n"
            "- 'Which episode has the line...', 'Find the scene where...'\n"
            "- Mapping transcript results back to recordings via DVR metadata\n\n"

            # ---- ENTITY EXTRACTION ----
            "ENTITY EXTRACTION:\n"
            "When the user mentions a show, reduce it to minimal search text "
            "(e.g. 'big bang theory', 'law order svu'). "
            "Search using the DVR MCP's search tool, select the canonical match, "
            "and use canonical IDs in the final tool call. "
            "If the match is ambiguous, ask the user.\n\n"

            # ---- TRANSCRIPT-AWARE ROUTING ----
            "TRANSCRIPT-AWARE ROUTING:\n"
            "If the user references quotes, scenes, or dialogue:\n"
            "1. Query transcript_search or transcript_cross_search\n"
            "2. Resolve transcript result to a recording_id and timestamp\n"
            "3. Use the correct DVR MCP to act on that recording "
            "(e.g. play at offset, delete, get metadata)\n\n"

            # ---- MULTI-MCP ORCHESTRATION PATTERNS ----
            "MULTI-MCP ORCHESTRATION PATTERNS:\n"
            "A) Delete recording: search DVR → canonicalize → find recording → delete via DVR MCP\n"
            "B) File size of recording: search DVR → get file path from metadata → linux_file_info(path)\n"
            "C) Orphaned file cleanup: get DVR file lists → get filesystem listing → compute orphans → delete via Linux MCP\n"
            "D) Transcript playback: transcript_search → resolve recording+timestamp → DVR play at offset\n\n"

            # ---- DISCOVERED PATHS ----
            "KNOWN PATHS:\n"
            "- SageTV recordings are stored at /var/media/tv\n"
            + channels_path_line +
            "- Both SageTV and Channels DVR use .vprj sidecar files alongside recordings (.mpg, .ts)\n\n"

            # ---- SAFETY RULES ----
            "SAFETY RULES:\n"
            "- NEVER delete DVR-managed files via Linux MCP. Use the DVR's own delete tool.\n"
            "- Tools marked CONFIRM require confirmation before executing destructive actions.\n"
            "- Tools marked DANGEROUS (server reboot/shutdown) should only be used if the user explicitly requests it.\n"
            "- Never hallucinate file paths. Retrieve paths from DVR metadata or discovered paths above.\n\n"

            # ---- OUTPUT RULES ----
            "OUTPUT RULES:\n"
            "1. When making a tool call, output ONLY the <tool_call> block — no other text.\n"
            "2. When giving your final answer, use plain conversational language.\n"
            "3. NEVER include raw JSON, tool names, or tool result data in your final answer.\n"
            "4. NEVER say 'Tool:', 'Result:', or reference tool names in your response.\n"
            "5. Summarize tool results in natural language.\n"
            "6. If a tool returns an error, briefly tell the user the service is offline.\n"
            "7. Do NOT retry a tool that returned an error.\n"
            "8. Keep answers concise — 1 to 3 sentences when possible.\n"
            "9. Only use tools listed below. Do not invent tool names.\n"
            "10. NEVER use placeholder values like '<path from ...>'. Use only real values you know.\n"
            "11. If the DVR target is ambiguous (SageTV vs ChannelsDVR), ask the user.\n"
            + TOOL_DEFINITIONS
        )

    async def run(
        self,
        user_query: str,
        transcript_context: str = "",
        semantic_context: str = "",
    ) -> Dict[str, Any]:
        """
        Run the agentic loop: send query to LLM, parse tool calls,
        execute them, feed results back, repeat until final answer.

        Returns:
            Dict with status, response, model, iterations.
        """
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": await self._build_system_prompt()},
        ]

        # Build user message with pre-fetched context
        context_parts = []
        if semantic_context:
            context_parts.append(semantic_context)
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

        for iteration in range(MAX_ITERATIONS):
            logger.info("Agent loop iteration %d/%d", iteration + 1, MAX_ITERATIONS)

            llm_result = await self._orch.llm.generate_chat(messages)
            if llm_result.get("error"):
                return llm_result

            response_text = llm_result.get("response", "")
            logger.info(
                "LLM response (iter %d, %d chars): %s",
                iteration + 1, len(response_text), response_text[:200],
            )

            tool_calls = self._parse_tool_calls(response_text)

            if not tool_calls:
                final = self._strip_markers(response_text)
                return {
                    "status": "ok",
                    "response": final,
                    "model": llm_result.get("model", ""),
                    "iterations": iteration + 1,
                }

            # Append assistant message, execute tools, feed back results
            messages.append({"role": "assistant", "content": response_text})

            tool_results: List[str] = []
            has_service_error = False
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                tool_args = tc.get("args") or {}
                logger.info("Executing tool: %s(%s)", tool_name, json.dumps(tool_args, default=str)[:200])

                result = await self._execute_tool(tool_name, tool_args)
                result_str = json.dumps(result, default=str)
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
        logger.warning("Agent loop reached max iterations (%d)", MAX_ITERATIONS)
        return {
            "status": "ok",
            "response": (
                "I wasn't able to fully resolve your request within the "
                "allowed steps. Here's what I found so far: "
                + llm_result.get("response", "")
            ),
            "model": llm_result.get("model", ""),
            "iterations": MAX_ITERATIONS,
        }

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    _TOOL_CALL_RE = re.compile(
        r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL,
    )

    def _parse_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        """Extract tool call JSON blocks from LLM response text."""
        matches = self._TOOL_CALL_RE.findall(text)
        calls: List[Dict[str, Any]] = []
        for match in matches:
            try:
                parsed = json.loads(match)
                if "tool" in parsed:
                    if "args" not in parsed:
                        parsed["args"] = {}
                    calls.append(parsed)
                else:
                    logger.warning("Tool call missing 'tool' key: %s", match[:200])
            except json.JSONDecodeError:
                logger.warning("Malformed tool call JSON: %s", match[:200])
        return calls

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
            reader, writer = await asyncio.open_connection("127.0.0.1", 8770)
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
