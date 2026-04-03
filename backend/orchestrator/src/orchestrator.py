"""
orchestrator.py
Core orchestration layer for the LLM Remote system.

Responsibilities:
- Initialize all subsystem services
- Provide high-level methods: run_query, run_playback, run_search, run_system, run_metadata
- Manage session state and service lifecycle
- Centralized logging and error boundaries
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict, Optional

from utils.logger import get_logger
from registry.commands import CommandRegistry
from services.llm import LLMService
from services.whisper import WhisperService
from services.tts import TTSService
from services.llm_pipeline import LLMPipeline
from services.playback import PlaybackService
from services.playback_state import PlaybackStateTracker
from services.playback_controller import PlaybackController
from services.search import SearchService
from services.metadata import MetadataService
from services.system import SystemService
from services.voice_session import VoiceSessionManager
from services.tool_router import ToolRouter
from services.ssd_extractor import SSDExtractor
from services.transcription_queue import TranscriptionQueue
from services.mcp_client import MCPClient

logger = get_logger(__name__)


class Orchestrator:
    """
    Central orchestrator that initializes all services and provides
    high-level execution methods for the transport layers.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config

        # Registry
        self.registry = CommandRegistry()

        # Core AI services
        self.llm = LLMService(
            model_path=config.get("llm", {}).get("model_path", "models/llm"),
        )
        self.whisper = WhisperService(
            model_path=config.get("whisper", {}).get("model_path", "models/whisper"),
            model_size=config.get("whisper", {}).get("model_size", "base"),
        )
        self.tts = TTSService(
            model_path=config.get("tts", {}).get("model_path", "models/tts"),
            output_dir=config.get("tts", {}).get("output_dir", "/tmp/tts_output"),
        )

        # Pipeline
        self.pipeline = LLMPipeline(
            whisper=self.whisper,
            llm=self.llm,
            tts=self.tts,
        )

        # Playback
        self.playback_service = PlaybackService(self)
        self.playback_state = PlaybackStateTracker(self, poll_interval=2.0)
        self.playback_controller = PlaybackController(
            self, self.playback_state, default_target="sagetv",
        )

        # Other services
        self.search = SearchService(self)
        self.metadata = MetadataService(self)
        self.system = SystemService()
        self.ssd_extractor = SSDExtractor(self.llm)

        # Voice sessions
        self.voice_sessions = VoiceSessionManager(
            pipeline=self.pipeline,
            llm=self.llm,
            max_history=config.get("voice", {}).get("max_history", 20),
            session_ttl=config.get("voice", {}).get("session_ttl", 1800.0),
        )

        # Tool router
        self.tool_router = ToolRouter(
            orchestrator=self,
            playback=self.playback_controller,
            search=self.search,
        )

        # Transcription queue
        self.transcription_queue = TranscriptionQueue(
            worker=self.whisper.transcribe,
        )

        # Backend MCP clients
        mcp = config.get("mcp", {})
        self._sagetv = MCPClient(
            host=mcp.get("sagetv_host", "127.0.0.1"),
            port=mcp.get("sagetv_port", 8766),
            name="sagetv",
        )
        self._channels = MCPClient(
            host=mcp.get("channels_host", "127.0.0.1"),
            port=mcp.get("channels_port", 8767),
            name="channels",
        )

        # Session manager URL for device → session_id resolution
        self._session_url = config.get("session_manager_url", "http://127.0.0.1:8769")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize all services and load models."""
        logger.info("Initializing orchestrator …")

        await self.llm.load()
        await self.whisper.load()
        await self.tts.load()
        await self.playback_state.start()
        await self.transcription_queue.start()

        self._register_default_commands()

        logger.info("Orchestrator initialized successfully")

    async def shutdown(self) -> None:
        """Gracefully shut down all services."""
        logger.info("Shutting down orchestrator …")

        await self.transcription_queue.stop()
        await self.playback_state.stop()
        await self.tts.unload()
        await self.whisper.unload()
        await self.llm.unload()
        await self._sagetv.close()
        await self._channels.close()

        logger.info("Orchestrator shutdown complete")

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def execute(self, command: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Unified command execution entry point.

        Args:
            command: Namespaced command string (e.g., "sagetv.play", "system.info").
            payload: Optional parameters dict.

        Returns:
            Dict containing result or error details.
        """
        logger.info("Execute: %s", command)
        try:
            namespace, action = command.split(".", 1)
        except ValueError:
            return {"error": f"Invalid command format: '{command}'"}

        try:
            if namespace == "sagetv":
                return await self._execute_sagetv(action, payload or {})
            if namespace == "channels":
                return await self._execute_channels(action, payload or {})
            if namespace == "system":
                return await self.system.execute(action, payload or {})
            if namespace == "llm":
                return await self.llm.generate(
                    payload.get("prompt", "") if payload else "", params=payload,
                )
            if namespace == "whisper":
                return await self.whisper.transcribe(
                    payload.get("audio_path", "") if payload else "",
                    options=payload,
                )
            if namespace == "tts":
                return await self.tts.synthesize(
                    payload.get("text", "") if payload else "",
                )

            # Fall through to registry
            handler = self.registry.resolve(command)
            if handler:
                return await handler(payload or {})

            return {"error": f"Unknown command '{command}'"}
        except Exception as exc:
            logger.exception("Command execution error: %s", command)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # High-level orchestration methods
    # ------------------------------------------------------------------

    async def run_query(
        self,
        prompt: str,
        synthesize: bool = True,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Run a text query through the LLM pipeline with transcript context."""
        logger.info("run_query (synthesize=%s)", synthesize)
        try:
            # Search transcripts for relevant content
            transcript_results = await self.search.transcript_search(prompt)
            transcript_hits = []
            if isinstance(transcript_results, dict):
                data = transcript_results.get("data", transcript_results)
                transcript_hits = data.get("results", [])

            # Inject transcript context if available
            transcript_context = ""
            if transcript_hits:
                lines = []
                for r in transcript_hits[:5]:
                    title = r.get("title", "Unknown")
                    ep = r.get("episode_title", "")
                    start = r.get("start_time", 0)
                    snippet = r.get("snippet", "").replace("<b>", "").replace("</b>", "")
                    mins = int(start // 60)
                    secs = int(start % 60)
                    time_str = f"{mins}:{secs:02d}"
                    if ep:
                        lines.append(f'From "{title}" - "{ep}" at {time_str}: {snippet}')
                    else:
                        lines.append(f'From "{title}" at {time_str}: {snippet}')
                transcript_context = "\n".join(lines)

            if transcript_context:
                enriched_prompt = (
                    f"Relevant transcript excerpts:\n{transcript_context}\n\n"
                    f"User query: {prompt}"
                )
            else:
                enriched_prompt = prompt

            llm_result = await self.pipeline.run_text_query(
                enriched_prompt, synthesize=synthesize, metadata=metadata,
            )

            # Attach transcript hits to the response for the frontend
            if isinstance(llm_result, dict):
                llm_result["transcript_results"] = transcript_hits

            return llm_result
        except Exception as exc:
            logger.exception("run_query failed")
            return {"error": str(exc)}

    async def run_query_voice(self, audio_path: str) -> Dict[str, Any]:
        """Run a voice query: audio → transcription → LLM → TTS."""
        logger.info("run_query_voice: %s", audio_path)
        try:
            return await self.pipeline.run_voice_query(audio_path)
        except Exception as exc:
            logger.exception("run_query_voice failed")
            return {"error": str(exc)}

    async def run_playback(
        self, action: str, target: str = "sagetv", payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Execute a playback action with device→session resolution."""
        logger.info("run_playback: %s.%s", target, action)
        try:
            payload = dict(payload or {})

            # Resolve device_id → session_id if present
            device_id = payload.pop("device_id", None)
            if device_id and "session_id" not in payload:
                ctx = await self.resolve_session(device_id)
                if ctx and ctx.get("session"):
                    session = ctx["session"]
                    payload["session_id"] = session.get("session_id", "")
                    # Override target system from resolved device if needed
                    resolved_system = ctx.get("system")
                    if resolved_system and resolved_system != "unknown":
                        target = resolved_system
                    logger.info("Resolved device %s → session_id=%s system=%s",
                                device_id, payload.get("session_id"), target)

            ctrl = self.playback_controller
            if action == "play":
                return await ctrl.play(target, payload)
            if action == "pause":
                return await ctrl.pause(target, payload)
            if action == "stop":
                return await ctrl.stop(target, payload)
            if action == "seek":
                position = payload.get("position", 0)
                return await ctrl.seek(position, target, payload)
            if action == "status":
                states = await ctrl.now_playing(target)
                return {k: v.to_dict() for k, v in states.items()}
            return {"error": f"Unknown playback action '{action}'"}
        except Exception as exc:
            logger.exception("run_playback failed")
            return {"error": str(exc)}

    async def run_search(self, query: str, target: str | None = None) -> Dict[str, Any]:
        """Search for programs."""
        logger.info("run_search: query=%s target=%s", query, target)
        try:
            if target:
                return await self.search.search_programs(target, query)
            return await self.search.search_all(query)
        except Exception as exc:
            logger.exception("run_search failed")
            return {"error": str(exc)}

    async def run_metadata(self, target: str, program_id: str) -> Dict[str, Any]:
        """Fetch program metadata."""
        logger.info("run_metadata: target=%s id=%s", target, program_id)
        try:
            return await self.metadata.get_program_info(target, program_id)
        except Exception as exc:
            logger.exception("run_metadata failed")
            return {"error": str(exc)}

    async def run_system(self, action: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Execute a system command."""
        logger.info("run_system: %s", action)
        try:
            return await self.system.execute(action, payload or {})
        except Exception as exc:
            logger.exception("run_system failed")
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Backend execution via MCP
    # ------------------------------------------------------------------

    # Maps short action names (used by orchestrator) to actual MCP tool names
    _SAGETV_TOOL_MAP = {
        "play": "sagetv_resume_playback",
        "pause": "sagetv_pause_playback",
        "stop": "sagetv_stop_playback",
        "seek": "sagetv_seek_absolute",
        "volume": "sagetv_set_volume",
        "mute": "sagetv_mute",
        "unmute": "sagetv_unmute",
        "skip_forward": "sagetv_skip_forward",
        "skip_back": "sagetv_skip_back",
        "status": "sagetv_get_now_playing",
        "tune": "sagetv_tune_channel",
        "get_recordings": "sagetv_get_recordings",
        "search": "sagetv_search_shows",
        "get_channels": "sagetv_get_channels",
        "record": "sagetv_record_show",
        "cancel_recording": "sagetv_cancel_recording",
        "delete": "sagetv_delete_media_file",
        "get_recording": "sagetv_get_recording",
        "get_airing": "sagetv_get_airing",
        "search_recordings": "sagetv_search_recordings",
        "recent_recordings": "sagetv_get_recent_recordings",
        "active_recordings": "sagetv_get_active_recordings",
        "set_watched": "sagetv_set_watched",
        "set_archived": "sagetv_set_archived",
        "set_property": "sagetv_set_media_file_property",
        "get_property": "sagetv_get_media_file_property",
        "play_pause": "sagetv_pause_playback",
        "commercial_skip": "sagetv_commercial_skip",
        "mute_toggle": "sagetv_mute",
    }

    _CHANNELS_TOOL_MAP = {
        "play": "channels_resume_playback",
        "pause": "channels_pause_playback",
        "stop": "channels_stop_playback",
        "seek": "channels_seek",
        "status": "channels_get_now_playing",
        "get_recordings": "channels_get_recordings",
        "search": "channels_search",
        "get_channels": "channels_get_channels",
        "delete": "channels_delete_recording",
        "play_pause": "channels_pause_playback",
        "skip_forward": "channels_skip_forward",
        "skip_back": "channels_skip_back",
        "commercial_skip": "channels_skip_commercial",
        "mute_toggle": "channels_mute",
    }

    async def _execute_sagetv(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Route SageTV commands through the MCP client."""
        tool_name = self._SAGETV_TOOL_MAP.get(action, f"sagetv_{action}")
        try:
            return await self._sagetv.call_tool(tool_name, payload)
        except ConnectionError as exc:
            logger.warning("SageTV MCP unavailable: %s", exc)
            return {"error": f"SageTV MCP unavailable: {exc}"}

    async def _execute_channels(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Route ChannelsDVR commands through the MCP client."""
        tool_name = self._CHANNELS_TOOL_MAP.get(action, f"channels_{action}")
        try:
            return await self._channels.call_tool(tool_name, payload)
        except ConnectionError as exc:
            logger.warning("ChannelsDVR MCP unavailable: %s", exc)
            return {"error": f"ChannelsDVR MCP unavailable: {exc}"}

    # ------------------------------------------------------------------
    # Session resolution
    # ------------------------------------------------------------------

    async def resolve_session(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Resolve device_id → session context via the Session Manager.
        Returns dict with session_id, system, device_name, etc.
        """
        if not device_id:
            return None
        import aiohttp
        url = f"{self._session_url}/sessions/resolve/{device_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data
                    logger.warning("Session resolve returned %d", resp.status)
                    return None
        except Exception as exc:
            logger.warning("Session resolution failed for %s: %s", device_id, exc)
            return None

    # ------------------------------------------------------------------
    # Default command registration
    # ------------------------------------------------------------------

    def _register_default_commands(self) -> None:
        """Register built-in commands in the registry."""
        self.registry.register("system", "info", "Get system information")
        self.registry.register("system", "diagnostics", "Get runtime diagnostics")
        self.registry.register("system", "volume", "Get or set volume")
        self.registry.register("system", "reboot", "Reboot the system")
        self.registry.register("system", "shutdown", "Shut down the system")
        self.registry.register("llm", "generate", "Generate LLM response")
        self.registry.register("whisper", "transcribe", "Transcribe audio")
        self.registry.register("tts", "synthesize", "Synthesize speech")
        logger.info("Default commands registered (%d total)", len(self.registry.list_commands()))
