"""Shared MCP tool discovery and OpenAI schema conversion."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


class MCPToolRegistry:
    """Discover and filter tool schemas for planner consumption."""

    _ESSENTIAL_TOOLS = {
        "channels_search_recordings", "channels_get_recordings",
        "channels_get_upcoming_recordings", "channels_get_now_playing",
        "channels_search_epg", "channels_get_scheduled_recordings",
        "channels_get_channels", "channels_get_storage_status",
        "channels_get_jobs", "channels_get_clients",
        "channels_list_genres",
        "sagetv_search_recordings", "sagetv_get_recordings",
        "sagetv_get_recent_recordings", "sagetv_get_active_recordings",
        "sagetv_get_upcoming_recordings", "sagetv_get_now_playing",
        "sagetv_search_shows", "sagetv_get_channels",
        "sagetv_get_disk_space", "sagetv_get_tuner_status",
        "sagetv_get_clients", "sagetv_get_recording",
        "sagetv_get_airing", "sagetv_get_show",
        "sagetv_list_genres",
        "linux_disk_usage", "linux_memory_info", "linux_uptime",
        "linux_service_status", "linux_docker_ps",
        "transcript_search", "transcript_cross_search",
        "transcript_actors", "transcript_stats",
        "transcript_get", "transcript_recording_summary",
        "transcript_list_recent",
    }

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
            "play", "pause", "stop", "seek", "set_volume", "mute", "unmute",
            "set_channel", "set_channel_by_number", "toggle_pause",
            "skip_commercial", "rewind", "status", "toggle_cc", "set_audio_track",
            "set_subtitle_track",
        },
        "system": {
            "disk_usage", "memory_info", "uptime", "service_status", "docker_ps",
            "get_storage_status", "get_jobs", "get_clients", "get_tuner_status",
        },
        "metadata": {
            "search_shows", "list_genres", "get_channels", "get_channel",
        },
        "transcript": {
            "search", "cross_search", "actors", "stats",
            "get", "recording_summary", "list_recent", "jobs", "reindex",
        },
    }

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
                "description": "Cross-metadata transcript search with optional filters.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Optional text query"},
                        "actor": {"type": "string", "description": "Actor name filter"},
                        "genre": {"type": "string", "description": "Genre filter"},
                        "channel": {"type": "string", "description": "Channel filter"},
                        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                        "system": {"type": "string", "description": "sagetv or channelsdvr"},
                        "limit": {"type": "integer", "description": "Max results"},
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
                        "actor_name": {"type": "string", "description": "Actor name"},
                        "limit": {"type": "integer", "description": "Max results"},
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
                    "properties": {"recording_id": {"type": "string"}},
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
                    "properties": {"recording_id": {"type": "string"}},
                    "required": ["recording_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transcript_list_recent",
                "description": "List recent transcripts ordered by date.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Number of transcripts"},
                    },
                },
            },
        },
    ]

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator

    @staticmethod
    def _mcp_to_openai_tool(t: Dict[str, Any]) -> Dict[str, Any]:
        schema = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": schema,
            },
        }

    def _tool_matches_domain(self, tool_name: str, domains: list[str]) -> bool:
        if not domains:
            return True
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

    async def discover_openai_tools(
        self,
        systems: list[str] | None = None,
        domains: list[str] | None = None,
        temporal: str = "",
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """Return filtered OpenAI tool schemas and full validation schemas."""
        all_systems = {"sagetv", "channelsdvr"}
        active = set(systems) if systems else all_systems
        domains = domains or []

        hide_for_future = {"search_recordings", "get_recordings", "get_recent_recordings"}
        hide_for_past = {"get_upcoming_recordings", "get_scheduled_recordings", "search_epg"}

        tools: List[Dict[str, Any]] = []
        schemas: Dict[str, Dict[str, Any]] = {}

        server_map = []
        if "channelsdvr" in active and hasattr(self._orch, "_channels"):
            server_map.append((self._orch._channels, "Channels DVR"))
        if "sagetv" in active and hasattr(self._orch, "_sagetv"):
            server_map.append((self._orch._sagetv, "SageTV"))
        if hasattr(self._orch, "_linux"):
            server_map.append((self._orch._linux, "Linux"))

        for client, label in server_map:
            try:
                mcp_tools = await client.list_tools()
                for t in mcp_tools:
                    schema = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
                    schemas[t["name"]] = schema
                    if t["name"] not in self._ESSENTIAL_TOOLS:
                        continue
                    if not self._tool_matches_domain(t["name"], domains):
                        continue
                    suffix = t["name"].split("_", 1)[1] if "_" in t["name"] else ""
                    if temporal == "future" and suffix in hide_for_future:
                        continue
                    if temporal == "past" and suffix in hide_for_past:
                        continue
                    tools.append(self._mcp_to_openai_tool(t))
                logger.info("Discovered %d OpenAI tools from %s", len(mcp_tools), label)
            except Exception as exc:
                logger.warning("Could not discover tools from %s: %s", label, exc)

        for t in self._TRANSCRIPT_TOOLS_OPENAI:
            fn = t["function"]
            if fn["name"] in self._ESSENTIAL_TOOLS:
                tools.append(t)
                schemas[fn["name"]] = fn.get("parameters", {})

        return tools, schemas
