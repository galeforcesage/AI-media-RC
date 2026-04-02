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

        # Backend client stubs (populated on init)
        self._sagetv: Any = None
        self._channels: Any = None

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
            # Inject transcript context if available
            transcript_context = await self.search.inject_transcript_context(prompt)
            if transcript_context:
                enriched_prompt = (
                    f"Relevant transcript excerpts:\n{transcript_context}\n\n"
                    f"User query: {prompt}"
                )
            else:
                enriched_prompt = prompt

            return await self.pipeline.run_text_query(
                enriched_prompt, synthesize=synthesize, metadata=metadata,
            )
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
        """Execute a playback action."""
        logger.info("run_playback: %s.%s", target, action)
        try:
            ctrl = self.playback_controller
            if action == "play":
                return await ctrl.play(target, payload)
            if action == "pause":
                return await ctrl.pause(target)
            if action == "stop":
                return await ctrl.stop(target)
            if action == "seek":
                position = (payload or {}).get("position", 0)
                return await ctrl.seek(position, target)
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
    # Backend execution stubs
    # ------------------------------------------------------------------

    async def _execute_sagetv(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Route SageTV commands through the MCP client when available."""
        if self._sagetv:
            return await self._sagetv.execute(action, payload)
        return {"stub": f"sagetv.{action}", "payload": payload}

    async def _execute_channels(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Route ChannelsDVR commands through the MCP client when available."""
        if self._channels:
            return await self._channels.execute(action, payload)
        return {"stub": f"channels.{action}", "payload": payload}

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
