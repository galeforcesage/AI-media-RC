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
from datetime import datetime, timedelta
import logging
import os
import re
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
from services.agent import AgentLoop
from services.semantic_index import SemanticIndex
from services.entity_context import EntityContextStore
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
            base_url=config.get("llm", {}).get("base_url", "http://127.0.0.1:11434"),
            model=config.get("llm", {}).get("model", "mistral:instruct"),
            num_threads=config.get("llm", {}).get("num_threads"),  # None = use LLMService dynamic default
            temperature=config.get("llm", {}).get("temperature", 0.7),
            num_predict=config.get("llm", {}).get("num_predict", 512),
            num_ctx=config.get("llm", {}).get("num_ctx", 4096),
            max_concurrent=config.get("llm", {}).get("max_concurrent", 1),
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

        # Semantic index for fast context retrieval
        self.semantic_index = SemanticIndex(orchestrator=self)

        # Agentic tool-calling loop
        self.agent = AgentLoop(orchestrator=self)

        # Conversation-scoped entity memory for multi-turn context
        self.entity_store = EntityContextStore(
            ttl=config.get("entity_ttl", 600.0),
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

        self._linux = MCPClient(
            host=mcp.get("linux_host", "127.0.0.1"),
            port=mcp.get("linux_port", 8768),
            name="linux",
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

        # Start semantic index (loads model + refreshes in background)
        await self.semantic_index.start()

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
        await self.semantic_index.stop()
        await self._linux.close()

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
    # Fast-path classifier: skip LLM entirely for simple commands
    # ------------------------------------------------------------------

    # Each entry: (compiled_regex, action, system_override | None, human_label)
    # The regex matches the FULL lowercased prompt.
    _FAST_PATTERNS: list[tuple[re.Pattern, str, str | None, str]] = [
        # Playback transport
        (re.compile(r"^(please\s+)?(pause|pause\s+(it|this|playback|the\s+(tv|show|video)))$", re.I),
         "pause", None, "Paused"),
        (re.compile(r"^(please\s+)?(play|resume|unpause|play\s+(it|this|playback))$", re.I),
         "play", None, "Resumed playback"),
        (re.compile(r"^(please\s+)?(stop|stop\s+(it|this|playback|the\s+(tv|show|video)))$", re.I),
         "stop", None, "Stopped"),
        (re.compile(r"^(please\s+)?(play[\s/]?pause|toggle\s+play)$", re.I),
         "play_pause", None, "Toggled play/pause"),

        # Skip / seek
        (re.compile(r"^(please\s+)?(skip|fast)\s*forward", re.I),
         "skip_forward", None, "Skipped forward"),
        (re.compile(r"^(please\s+)?(skip|rewind)\s*back", re.I),
         "skip_back", None, "Skipped back"),
        (re.compile(r"^(please\s+)?(skip\s*(the\s+)?commercial(s)?|comskip)$", re.I),
         "commercial_skip", None, "Skipping commercial"),

        # Volume / mute
        (re.compile(r"^(please\s+)?mute(\s+(it|the\s+(tv|sound)))?$", re.I),
         "mute_toggle", None, "Toggled mute"),
        (re.compile(r"^(please\s+)?unmute(\s+(it|the\s+(tv|sound)))?$", re.I),
         "unmute", None, "Unmuted"),

        # Channel
        (re.compile(r"^(please\s+)?channel\s*up$", re.I),
         "channel_up", None, "Channel up"),
        (re.compile(r"^(please\s+)?channel\s*down$", re.I),
         "channel_down", None, "Channel down"),

        # Navigation (SageTV only)
        (re.compile(r"^(please\s+)?(go\s+)?home$", re.I),
         "open_home", "sagetv", "Opening Home"),
        (re.compile(r"^(please\s+)?(open\s+)?(the\s+)?(program\s+)?guide$", re.I),
         "open_guide", "sagetv", "Opening Guide"),
        (re.compile(r"^(please\s+)?(show\s+)?(my\s+)?recordings$", re.I),
         "open_recordings", "sagetv", "Opening Recordings"),
        (re.compile(r"^(please\s+)?(go\s+to\s+|open\s+)?live\s*tv$", re.I),
         "open_live_tv", "sagetv", "Opening Live TV"),
        (re.compile(r"^(please\s+)?(go\s*)?back$", re.I),
         "nav_back", "sagetv", "Going back"),
        (re.compile(r"^(please\s+)?(exit|close)(\s+(it|this|the\s+(app|video|player)))?$", re.I),
         "close", "sagetv", "Closing"),

        # CC
        (re.compile(r"^(please\s+)?(toggle\s+)?(closed\s*)?captions?$", re.I),
         "toggle_cc", None, "Toggled captions"),
        (re.compile(r"^(please\s+)?(turn\s+)?(on|off)\s+(closed\s*)?captions?$", re.I),
         "toggle_cc", None, "Toggled captions"),
        (re.compile(r"^(please\s+)?cc$", re.I),
         "toggle_cc", None, "Toggled captions"),
    ]

    def _try_fast_path(
        self, prompt: str, systems: list[str] | None,
    ) -> tuple[str, str, str | None] | None:
        """
        Check if the prompt matches a simple command pattern.

        Returns (action, label, system_override) or None if no match.
        """
        text = prompt.strip()
        for pattern, action, sys_override, label in self._FAST_PATTERNS:
            if pattern.match(text):
                logger.info("Fast-path match: '%s' → %s", text, action)
                return action, label, sys_override
        return None

    # ------------------------------------------------------------------
    # Temporal intent classifier
    # ------------------------------------------------------------------

    _FUTURE_RE = re.compile(
        r"\b(?:record(?:s|ing|ed)?\s+(?:today|tonight|tomorrow|this\s+week|next\s+\w+)|"
        r"what(?:'s| is| will)\s+(?:record|schedule|on\s+tonight)|"
        r"upcoming|scheduled|will\s+record|"
        r"next\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
        r"what\s+records\b|set\s+(?:a\s+)?recording|schedule\s+recording)\b",
        re.IGNORECASE,
    )
    _PAST_RE = re.compile(
        r"\b(?:recorded|watched|transcript|was\s+on|aired|"
        r"yesterday|last\s+(?:night|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
        r"what\s+(?:has\s+)?recorded|past\s+\d+\s+days?)\b",
        re.IGNORECASE,
    )
    _PRESENT_RE = re.compile(
        r"\b(?:right\s+now|currently|is\s+recording|now\s+playing|"
        r"what(?:'s| is)\s+(?:on\s+now|playing|airing))\b",
        re.IGNORECASE,
    )

    def _classify_temporal(self, prompt: str) -> str:
        """Classify query temporal intent: 'past', 'future', 'present', or 'both'."""
        has_future = bool(self._FUTURE_RE.search(prompt))
        has_past = bool(self._PAST_RE.search(prompt))
        has_present = bool(self._PRESENT_RE.search(prompt))
        if has_future and not has_past:
            return "future"
        if has_past and not has_future:
            return "past"
        if has_present and not has_past and not has_future:
            return "present"
        return "both"

    # ------------------------------------------------------------------
    # Domain classifier for tool subsetting
    # ------------------------------------------------------------------

    _DOMAIN_PATTERNS = {
        "recordings": re.compile(
            r"\b(?:record|episode|show|series|movie|film|watch|"
            r"recorded|unwatched|dvr|aired|season\b.*episode)\b", re.I),
        "schedule": re.compile(
            r"\b(?:upcom|schedul|tonight|today|what.s on|epg|guide|"
            r"program\s*guide|airing|will\s+record|set\s+record|"
            r"record(?:s|ing)?\s+(?:today|tonight|tomorrow|this\s+week|next\s+\w+)|"
            r"next\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
            r"this\s+week|"
            r"auto.record|subscribe)\b", re.I),
        "playback": re.compile(
            r"\b(?:play|pause|stop|skip|seek|rewind|fast.forward|"
            r"volume|mute|unmute|channel\s*up|channel\s*down|"
            r"commercial|resume|live\s*tv|tune)\b", re.I),
        "system": re.compile(
            r"\b(?:disk|memory|uptime|service|docker|container|"
            r"log|reboot|shutdown|restart|nginx|storage|cpu)\b", re.I),
        "metadata": re.compile(
            r"\b(?:genre|channel|actor|cast|rating|"
            r"how many|count|list\s+genre|what\s+genre)\b", re.I),
        "transcript": re.compile(
            r"\b(?:transcript|said|quote|dialogue|mention|"
            r"spoken|word|subtitle|caption)\b", re.I),
    }

    def _classify_domain(self, prompt: str) -> list[str]:
        """Classify query into one or more tool domains for subsetting."""
        domains = []
        for domain, pattern in self._DOMAIN_PATTERNS.items():
            if pattern.search(prompt):
                domains.append(domain)
        return domains if domains else ["recordings", "schedule", "metadata"]

    # ------------------------------------------------------------------
    # High-level orchestration methods
    # ------------------------------------------------------------------

    # Week-range patterns handled deterministically (dateparser returns
    # single dates for these, but MCP tools need start..end ranges).
    _WEEK_RANGE_RE = re.compile(
        r"\b(last|past|this\s+past|previous)\s+week\b", re.IGNORECASE
    )
    _THIS_WEEK_RE = re.compile(r"\bthis\s+week\b", re.IGNORECASE)
    _NEXT_WEEK_RE = re.compile(r"\bnext\s+week\b", re.IGNORECASE)
    _LAST_N_DAYS_RE = re.compile(
        r"\b(?:last|past)\s+(\d+)\s+days?\b", re.IGNORECASE
    )

    # dateparser settings — base config shared by all parses.
    _DP_BASE = {
        "RETURN_AS_TIMEZONE_AWARE": False,
        "DATE_ORDER": "MDY",                 # US-style for numeric dates
    }

    def _week_bounds(self, ref: datetime, offset_weeks: int) -> tuple[str, str]:
        """Return (start, end) YYYY-MM-DD for a Sun-Sat week relative to ref."""
        # Sunday = 6 in Python's weekday() (Mon=0). Shift so Sunday is day 0.
        days_since_sun = (ref.weekday() + 1) % 7
        sun = ref - timedelta(days=days_since_sun)  # this Sunday
        sun += timedelta(weeks=offset_weeks)
        sat = sun + timedelta(days=6)
        return sun.strftime("%Y-%m-%d"), sat.strftime("%Y-%m-%d")

    # Phrases that signal the user is looking backward in time.
    _PAST_HINT_RE = re.compile(
        r"\b(?:last|past|previous|ago|yesterday|recorded|was)\b", re.IGNORECASE
    )

    def _resolve_dates(self, prompt: str) -> str:
        """Replace relative date expressions with concrete YYYY-MM-DD dates.

        Uses the ``dateparser`` library for broad NL coverage (yesterday,
        last Monday, next Friday, April 25th, 04/25/26, 2 days ago, …).
        Week-range phrases are handled deterministically because MCP tools
        need start..end pairs.
        """
        from dateparser.search import search_dates

        now = datetime.now()

        # ── 1) Deterministic week ranges ─────────────────────────
        m = self._LAST_N_DAYS_RE.search(prompt)
        if m:
            n = int(m.group(1))
            s = (now - timedelta(days=n)).strftime("%Y-%m-%d")
            e = now.strftime("%Y-%m-%d")
            prompt = self._LAST_N_DAYS_RE.sub(
                f"{m.group(0)} ({s} to {e})", prompt, count=1)
            return prompt  # range phrases are exclusive — skip NL parse

        m = self._WEEK_RANGE_RE.search(prompt)
        if m:
            s, e = self._week_bounds(now, -1)
            prompt = self._WEEK_RANGE_RE.sub(
                f"{m.group(0)} ({s} to {e})", prompt, count=1)
            return prompt

        m = self._THIS_WEEK_RE.search(prompt)
        if m:
            s, e = self._week_bounds(now, 0)
            prompt = self._THIS_WEEK_RE.sub(
                f"{m.group(0)} ({s} to {e})", prompt, count=1)
            return prompt

        m = self._NEXT_WEEK_RE.search(prompt)
        if m:
            s, e = self._week_bounds(now, 1)
            prompt = self._NEXT_WEEK_RE.sub(
                f"{m.group(0)} ({s} to {e})", prompt, count=1)
            return prompt

        # ── 2) General NL date parsing via dateparser ────────────
        # Choose direction based on whether the prompt looks backward.
        prefer = "past" if self._PAST_HINT_RE.search(prompt) else "future"
        settings = {**self._DP_BASE, "PREFER_DATES_FROM": prefer}

        try:
            results = search_dates(
                prompt,
                settings=settings,
                languages=["en"],
            )
        except Exception:
            results = None

        if not results:
            # Fallback: common time-of-day phrases that dateparser misses
            _fallback_phrases = [
                (re.compile(r"\blast\s+night\b", re.I), -1),      # last night → yesterday
                (re.compile(r"\btonight\b", re.I), 0),            # tonight → today
                (re.compile(r"\bthis\s+morning\b", re.I), 0),     # this morning → today
                (re.compile(r"\bthis\s+afternoon\b", re.I), 0),   # this afternoon → today
                (re.compile(r"\bthis\s+evening\b", re.I), 0),     # this evening → today
                (re.compile(r"\bearlier\s+today\b", re.I), 0),    # earlier today → today
                (re.compile(r"\blast\s+evening\b", re.I), -1),    # last evening → yesterday
            ]
            for pat, day_offset in _fallback_phrases:
                m = pat.search(prompt)
                if m:
                    date_str = (now + timedelta(days=day_offset)).strftime("%Y-%m-%d")
                    prompt = prompt[:m.end()] + f" ({date_str})" + prompt[m.end():]
                    break  # only annotate the first match
            return prompt

        # Annotate every found expression with (YYYY-MM-DD), working
        # right-to-left so string offsets stay valid.
        already_annotated = set()
        for text_found, dt_found in reversed(results):
            if text_found in already_annotated:
                continue
            # Skip if dateparser returned a nonsensical match (single digit, etc.)
            if len(text_found.strip()) < 3:
                continue
            date_str = dt_found.strftime("%Y-%m-%d")
            # Only annotate first occurrence
            idx = prompt.rfind(text_found)
            if idx == -1:
                continue
            end = idx + len(text_found)
            # Don't double-annotate if already has (YYYY-
            if end < len(prompt) and prompt[end:end + 2] == " (":
                continue
            prompt = prompt[:end] + f" ({date_str})" + prompt[end:]
            already_annotated.add(text_found)

        return prompt

    async def run_query(
        self,
        prompt: str,
        synthesize: bool = True,
        metadata: Dict[str, Any] | None = None,
        systems: list[str] | None = None,
        status_callback=None,
        token_callback=None,
    ) -> Dict[str, Any]:
        """Run a text query through the agentic tool-calling loop."""
        # Pre-resolve relative date references so the LLM gets concrete dates
        prompt = self._resolve_dates(prompt)
        logger.info("run_query (synthesize=%s, systems=%s)", synthesize, systems)

        # ── Fast-path: skip LLM entirely for simple playback commands ──
        fast = self._try_fast_path(prompt, systems)
        if fast:
            action, label, sys_override = fast
            if status_callback:
                await status_callback(label)
            # Determine target system
            active = systems or ["sagetv", "channelsdvr"]
            target = sys_override or active[0]
            try:
                result = await self.run_playback(action, target=target)
                if result.get("error"):
                    return {
                        "status": "error",
                        "llm_response": f"Command failed: {result['error']}",
                        "transcript_results": [],
                    }
                return {
                    "status": "ok",
                    "llm_response": f"{label}.",
                    "transcript_results": [],
                    "fast_path": True,
                }
            except Exception as exc:
                logger.exception("Fast-path execution failed")
                return {"status": "error", "llm_response": f"Command failed: {exc}", "transcript_results": []}

        try:
            # Classify temporal intent to skip irrelevant pre-fetches
            temporal = self._classify_temporal(prompt)
            logger.info("Temporal intent: %s", temporal)

            # Classify domain for tool subsetting
            domains = self._classify_domain(prompt)
            logger.info("Domain classification: %s", domains)

            # Pre-fetch transcript context (PAST only — transcripts can't exist for future content)
            transcript_context = ""
            transcript_hits: list = []
            if temporal not in ("future", "present"):
                try:
                    if status_callback:
                        await status_callback("Searching transcripts")
                    transcript_results = await self.search.transcript_search(prompt)
                    if isinstance(transcript_results, dict):
                        data = transcript_results.get("data", transcript_results)
                        transcript_hits = data.get("results", [])
                    if transcript_hits:
                        lines = []
                        for r in transcript_hits[:2]:
                            title = r.get("title", "Unknown")
                            ep = r.get("episode_title", "")
                            start = r.get("start_time", 0)
                            snippet = r.get("snippet", "").replace("<b>", "").replace("</b>", "")[:150]
                            mins = int(start // 60)
                            secs = int(start % 60)
                            time_str = f"{mins}:{secs:02d}"
                            if ep:
                                lines.append(f'From "{title}" - "{ep}" at {time_str}: {snippet}')
                            else:
                                lines.append(f'From "{title}" at {time_str}: {snippet}')
                        transcript_context = "\n".join(lines)
                except Exception:
                    logger.warning("Transcript pre-fetch failed, continuing without")

            # Pre-fetch semantic context from the vector index (sub-second)
            # Pre-fetch semantic context (skip for future-only — no past media to match)
            semantic_context = ""
            if temporal != "future":
                try:
                    if self.semantic_index.ready:
                        if status_callback:
                            await status_callback("Searching media library")
                        hits = await self.semantic_index.search(prompt, n_results=2, systems=systems)
                        semantic_context = self.semantic_index.format_context(hits)
                        if semantic_context:
                            logger.info("Semantic index returned %d hits", len(hits))
                except Exception:
                    logger.warning("Semantic pre-fetch failed, continuing without")

            # Run the agentic tool-calling loop
            agent_result = await self.agent.run(
                prompt,
                transcript_context=transcript_context,
                semantic_context=semantic_context,
                systems=systems,
                temporal=temporal,
                domains=domains,
                entity_store=self.entity_store,
                status_callback=status_callback,
                token_callback=token_callback,
            )

            # Build response in pipeline-compatible format
            llm_result: Dict[str, Any] = {
                "status": agent_result.get("status", "ok"),
                "llm_response": agent_result.get("response", ""),
                "iterations": agent_result.get("iterations", 1),
            }

            # Optional TTS synthesis
            if synthesize and agent_result.get("status") == "ok":
                response_text = agent_result.get("response", "")
                if response_text:
                    tts_result = await self.tts.synthesize(response_text)
                    if tts_result.get("status") == "ok":
                        llm_result["audio_path"] = tts_result["audio_path"]

            # Attach transcript hits for the frontend
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

            # Route Channels DVR actions directly through MCP (supports all actions + bridge)
            if target in ("channelsdvr", "channels"):
                return await self._execute_channels(action, payload)

            # Resolve device_id → session_id if present (SageTV path)
            device_id = payload.pop("device_id", None)
            if device_id and "session_id" not in payload:
                # SageTV context devices: extract context ID directly (skip HTTP resolve)
                if device_id.startswith("sagetv-ctx-"):
                    payload["session_id"] = device_id[len("sagetv-ctx-"):]
                    logger.info("SageTV context device %s → session_id=%s",
                                device_id, payload["session_id"])
                else:
                    ctx = await self.resolve_session(device_id)
                    if ctx and ctx.get("session"):
                        session = ctx["session"]
                        payload["session_id"] = session.get("session_id", "")
                        resolved_system = ctx.get("system")
                        if resolved_system and resolved_system != "unknown":
                            target = resolved_system
                        logger.info("Resolved device %s → session_id=%s system=%s",
                                    device_id, payload.get("session_id"), target)

            # SageTV: route through MCP directly (supports all actions)
            return await self._execute_sagetv(action, payload)
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
        """Execute a system command via MCP Linux (or locally for RC services)."""
        logger.info("run_system: %s", action)
        try:
            if action == "restart_rc_service":
                return await self._restart_rc_service(payload or {})
            return await self._execute_linux(action, payload or {})
        except Exception as exc:
            logger.exception("run_system failed")
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # AI-media-RC service restart (local, not routed through MCP)
    # ------------------------------------------------------------------

    _RC_HOME = os.path.expanduser("~/AI-media-RC/backend")
    _RC_SERVICES = {
        "mcp-sagetv":      {"port": 8766, "cwd": f"{_RC_HOME}/mcp-sagetv",      "cmd": ".venv/bin/python main.py"},
        "mcp-channels":    {"port": 8767, "cwd": f"{_RC_HOME}/mcp-channels",    "cmd": ".venv/bin/python main.py"},
        "mcp-linux":       {"port": 8768, "cwd": f"{_RC_HOME}/mcp-linux",       "cmd": ".venv/bin/python main.py"},
        "session-manager": {"port": 8769, "cwd": f"{_RC_HOME}/session-manager", "cmd": ".venv/bin/python main.py"},
        "transcription":   {"port": 8770, "cwd": f"{_RC_HOME}/transcription",   "cmd": ".venv/bin/python main.py"},
    }

    async def _restart_rc_service(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Kill an AI-media-RC process by port and relaunch it."""
        service = payload.get("service", "")
        if service not in self._RC_SERVICES:
            return {"error": f"Unknown RC service '{service}'",
                    "allowed": sorted(self._RC_SERVICES.keys())}
        spec = self._RC_SERVICES[service]
        port = spec["port"]

        # 1. Kill existing process
        proc = await asyncio.create_subprocess_exec(
            "fuser", "-k", f"{port}/tcp",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        await asyncio.sleep(1)

        # 2. Relaunch — fully detach from this process
        shell_cmd = (
            f"cd {spec['cwd']} && "
            f"nohup {spec['cmd']} > /tmp/{service}.log 2>&1 </dev/null &"
        )
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", shell_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)

        # 3. Verify
        await asyncio.sleep(2)
        check = await asyncio.create_subprocess_exec(
            "fuser", f"{port}/tcp",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await check.communicate()
        if check.returncode != 0:
            return {"error": f"{service} started but not listening on port {port}"}

        return {"status": "ok", "service": service, "port": port}

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
        "play_pause": "sagetv_toggle_playback",
        "commercial_skip": "sagetv_commercial_skip",
        "mute_toggle": "sagetv_mute",
        "channel_up": "sagetv_channel_up",
        "channel_down": "sagetv_channel_down",
        "nav_up": "sagetv_nav_up",
        "nav_down": "sagetv_nav_down",
        "nav_left": "sagetv_nav_left",
        "nav_right": "sagetv_nav_right",
        "nav_select": "sagetv_nav_select",
        "nav_back": "sagetv_nav_back",
        "nav_options": "sagetv_nav_options",
        "page_up": "sagetv_page_up",
        "page_down": "sagetv_page_down",
        "toggle_cc": "sagetv_toggle_cc",
        "close": "sagetv_close",
        "power_off": "sagetv_power_off",
        "open_home": "sagetv_open_home",
        "open_guide": "sagetv_open_guide",
        "open_recordings": "sagetv_open_recordings",
        "open_live_tv": "sagetv_open_live_tv",
    }

    _CHANNELS_TOOL_MAP = {
        "play": "channels_resume_playback",
        "pause": "channels_pause_playback",
        "stop": "channels_stop_playback",
        "seek": "channels_seek_relative",
        "status": "channels_get_playback_status",
        "get_recordings": "channels_get_recordings",
        "search": "channels_search_recordings",
        "get_channels": "channels_get_channels",
        "delete": "channels_delete_recording",
        "play_pause": "channels_toggle_pause",
        "skip_forward": "channels_seek_forward",
        "skip_back": "channels_seek_backward",
        "commercial_skip": "channels_skip_commercial",
        "mute_toggle": "channels_toggle_mute",
        "play_channel": "channels_play_channel",
        "play_recording": "channels_play_recording",
        "channel_up": "channels_channel_up",
        "channel_down": "channels_channel_down",
        "toggle_cc": "channels_toggle_cc",
        "upcoming": "channels_get_upcoming_recordings",
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

    # Maps short system action names to MCP Linux tool names
    _LINUX_TOOL_MAP = {
        "info": "linux_uptime",
        "diagnostics": "linux_memory_info",
        "disk_usage": "linux_disk_usage",
        "memory": "linux_memory_info",
        "uptime": "linux_uptime",
        "network": "linux_network_info",
        "docker_status": "linux_docker_ps",
        "docker_restart": "linux_docker_restart",
        "docker_logs": "linux_docker_logs",
        "restart_service": "linux_restart_service",
        "restart_container": "linux_docker_restart",
        "restart_nginx": "linux_restart_nginx",
        "tail_log": "linux_tail_log",
        "list_directory": "linux_list_directory",
        "file_info": "linux_file_info",
        "find_large_files": "linux_find_large_files",
        "count_files": "linux_count_files",
        "reboot": "linux_reboot_server",
        "shutdown": "linux_shutdown_server",
        "service_status": "linux_service_status",
    }

    async def _execute_linux(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Route system/Linux commands through the MCP Linux client."""
        tool_name = self._LINUX_TOOL_MAP.get(action, f"linux_{action}")
        try:
            return await self._linux.call_tool(tool_name, payload)
        except ConnectionError as exc:
            logger.warning("Linux MCP unavailable: %s", exc)
            return {"error": f"Linux MCP unavailable: {exc}"}

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
