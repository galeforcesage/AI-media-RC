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
from services.openclaw_planner import OpenClawPlanner
from services.planner_registry import PlannerRegistry
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
        # Shared-GPU arbiter: lets the LLM path pause batch Whisper, pick the
        # biggest model VSR left room for, and gate lower-priority tenants
        # (Paperless) via HTTP. Optional config under "gpu_arbiter".
        from services.gpu_arbiter import GpuArbiter
        self.gpu_arbiter = GpuArbiter(config.get("gpu_arbiter"))
        self.llm.set_arbiter(self.gpu_arbiter)
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
        self._planner_registry = PlannerRegistry(self)
        self._planner_registry.register("agentloop", lambda: self.agent)
        self._planner_registry.register(
            "openclaw",
            lambda: OpenClawPlanner(orchestrator=self, fallback_planner=self.agent),
        )

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

    def _resolve_planner_name(self, metadata: Dict[str, Any] | None = None) -> str:
        """Resolve planner name from request metadata, then config defaults."""
        requested = None
        if isinstance(metadata, dict):
            requested = metadata.get("planner")
        if isinstance(requested, str) and requested.strip():
            return requested.strip().lower()
        return str(self.config.get("agent", {}).get("planner", "agentloop")).strip().lower()

    def _get_planner(self, metadata: Dict[str, Any] | None = None):
        """Get planner instance with fallback to agentloop if unknown."""
        planner_name = self._resolve_planner_name(metadata)
        try:
            planner = self._planner_registry.get(planner_name)
            if planner_name != "agentloop":
                logger.info("Using planner '%s'", planner_name)
            return planner
        except ValueError:
            logger.warning("Unknown planner '%s', falling back to 'agentloop'", planner_name)
            return self._planner_registry.get("agentloop")

    def _resolve_shadow_planner_name(self, metadata: Dict[str, Any] | None = None) -> str | None:
        """Resolve optional shadow planner from request metadata."""
        if not isinstance(metadata, dict):
            return None
        raw = metadata.get("shadow_planner")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
        return None

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
        r"next\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
        r"what\s+records\b|set\s+(?:a\s+)?recording|schedule\s+recording)\b",
        re.IGNORECASE,
    )
    _PAST_RE = re.compile(
        r"\b(?:recorded|watched|transcript|was\s+on|aired|"
        r"yesterday|last\s+(?:night|week|month|year|\d+\s+(?:days?|weeks?|months?))|"
        r"this\s+(?:year|month)|over\s+this\s+year|"
        r"past\s+(?:\d+\s+)?(?:days?|weeks?|months?|year)|"
        r"what\s+(?:has\s+)?recorded)\b",
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
            r"next\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
            r"this\s+(?:week|month|year)|last\s+(?:week|month|year)|"
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
            r"\b(?:transcripts?|said|says?|quote|dialogue|mention|"
            r"spoken|words?\s+(?:in|from|during)|subtitle|caption|"
            r"what\s+(?:was|were)\s+said|who\s+said|"
            r"did\s+\S+\s+say)\b", re.I),
    }

    # ── Hard-route patterns ──
    # A1: Metadata-only — episode facts, cast, dates (no dialogue needed)
    _METADATA_ONLY_RE = re.compile(
        r"\b(?:air\s*date|released|premiered|episode\s*list|"
        r"how\s+many\s+episodes?|season\s+count|how\s+many\s+seasons?|"
        r"cast|actors?|actress|director|writer|imdb|"
        r"which\s+episode|what\s+episode|episode\s+name|episode\s+title|"
        r"is\s+there\s+an?\s+episode\s+(?:where|about|with)|"
        r"when\s+(?:did|does|was)\s+\S+\s+(?:air|premiere|release|start)|"
        r"what\s+(?:year|date)\s+(?:did|was))\b", re.I)

    # A2: Transcript quote — user wants exact dialogue/words
    _TRANSCRIPT_QUOTE_RE = re.compile(
        r"\b(?:quote|exact\s+words?|(?:what|who|where)\s+did\s+\S+\s+say|"
        r"did\s+(?:they|he|she|someone|anyone|\S+)\s+say|"
        r"verbatim|dialogue|transcript\s+says?|"
        r"line\s+(?:from|in|about)|who\s+said|what\s+(?:was|were)\s+said|"
        r"in\s+which\s+(?:show|episode)\s+(?:did|do|does)|"
        r"word.for.word|spoken\s+(?:line|word))\b", re.I)

    def _classify_domain(self, prompt: str) -> list[str]:
        """Classify query into one or more tool domains for subsetting.

        Hard routes override soft pattern matching:
        - Transcript quote mode: user wants exact dialogue → transcript only
        - Metadata-only mode: episode facts/cast with no dialogue ask → metadata + recordings
        """
        # ── Hard route: transcript quote mode ──
        if self._TRANSCRIPT_QUOTE_RE.search(prompt):
            logger.info("Hard route → transcript (quote mode)")
            return ["transcript"]

        # ── Hard route: metadata-only ──
        if self._METADATA_ONLY_RE.search(prompt) and not self._DOMAIN_PATTERNS["transcript"].search(prompt):
            logger.info("Hard route → metadata-only")
            return ["metadata", "recordings"]

        # ── Soft pattern matching ──
        domains = []
        for domain, pattern in self._DOMAIN_PATTERNS.items():
            if pattern.search(prompt):
                domains.append(domain)
        return domains if domains else ["recordings", "schedule", "metadata"]

    async def _check_transcript_dependency(self, query: str) -> bool:
        """Ask the LLM a cheap yes/no: does this query require episode dialogue?"""
        classification_prompt = (
            "Does answering this question REQUIRE viewing episode dialogue or transcripts? "
            "Answer only yes or no.\n\n"
            f"Question: {query}"
        )
        try:
            result = await self.llm.generate(
                classification_prompt,
                params={"num_predict": 10, "temperature": 0.0},
            )
            answer = result.get("response", "").strip().lower()
            return answer.startswith("yes")
        except Exception:
            logger.warning("Transcript dependency check failed, skipping")
            return False

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
    _THIS_YEAR_RE = re.compile(
        r"\b(?:this\s+year|over\s+this\s+year|all\s+(?:this\s+)?year)\b", re.IGNORECASE
    )
    _LAST_YEAR_RE = re.compile(r"\blast\s+year\b", re.IGNORECASE)
    _NEXT_YEAR_RE = re.compile(r"\bnext\s+year\b", re.IGNORECASE)
    _THIS_MONTH_RE = re.compile(r"\bthis\s+month\b", re.IGNORECASE)
    _LAST_MONTH_RE = re.compile(r"\blast\s+month\b", re.IGNORECASE)
    _NEXT_MONTH_RE = re.compile(r"\bnext\s+month\b", re.IGNORECASE)
    _LAST_N_DAYS_RE = re.compile(
        r"\b(?:last|past)\s+(\d+)\s+days?\b", re.IGNORECASE
    )
    _LAST_N_WEEKS_RE = re.compile(
        r"\b(?:last|past)\s+(\d+)\s+weeks?\b", re.IGNORECASE
    )
    _LAST_N_MONTHS_RE = re.compile(
        r"\b(?:last|past)\s+(\d+)\s+months?\b", re.IGNORECASE
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

        m = self._THIS_YEAR_RE.search(prompt)
        if m:
            s = f"{now.year}-01-01"
            e = now.strftime("%Y-%m-%d")
            prompt = prompt[:m.end()] + f" ({s} to {e})" + prompt[m.end():]
            return prompt

        m = self._LAST_YEAR_RE.search(prompt)
        if m:
            s = f"{now.year - 1}-01-01"
            e = f"{now.year - 1}-12-31"
            prompt = prompt[:m.end()] + f" ({s} to {e})" + prompt[m.end():]
            return prompt

        m = self._THIS_MONTH_RE.search(prompt)
        if m:
            s = now.strftime("%Y-%m-01")
            e = now.strftime("%Y-%m-%d")
            prompt = prompt[:m.end()] + f" ({s} to {e})" + prompt[m.end():]
            return prompt

        m = self._LAST_MONTH_RE.search(prompt)
        if m:
            first_of_this = now.replace(day=1)
            last_month_end = first_of_this - timedelta(days=1)
            last_month_start = last_month_end.replace(day=1)
            s = last_month_start.strftime("%Y-%m-%d")
            e = last_month_end.strftime("%Y-%m-%d")
            prompt = prompt[:m.end()] + f" ({s} to {e})" + prompt[m.end():]
            return prompt

        m = self._NEXT_MONTH_RE.search(prompt)
        if m:
            # First day of next month
            if now.month == 12:
                nm_start = now.replace(year=now.year + 1, month=1, day=1)
            else:
                nm_start = now.replace(month=now.month + 1, day=1)
            # Last day of next month
            if nm_start.month == 12:
                nm_end = nm_start.replace(day=31)
            else:
                nm_end = nm_start.replace(month=nm_start.month + 1, day=1) - timedelta(days=1)
            s = nm_start.strftime("%Y-%m-%d")
            e = nm_end.strftime("%Y-%m-%d")
            prompt = prompt[:m.end()] + f" ({s} to {e})" + prompt[m.end():]
            return prompt

        m = self._NEXT_YEAR_RE.search(prompt)
        if m:
            s = f"{now.year + 1}-01-01"
            e = f"{now.year + 1}-12-31"
            prompt = prompt[:m.end()] + f" ({s} to {e})" + prompt[m.end():]
            return prompt

        m = self._LAST_N_WEEKS_RE.search(prompt)
        if m:
            n = int(m.group(1))
            s = (now - timedelta(weeks=n)).strftime("%Y-%m-%d")
            e = now.strftime("%Y-%m-%d")
            prompt = prompt[:m.end()] + f" ({s} to {e})" + prompt[m.end():]
            return prompt

        m = self._LAST_N_MONTHS_RE.search(prompt)
        if m:
            n = int(m.group(1))
            # Approximate: 30 days per month
            s = (now - timedelta(days=n * 30)).strftime("%Y-%m-%d")
            e = now.strftime("%Y-%m-%d")
            prompt = prompt[:m.end()] + f" ({s} to {e})" + prompt[m.end():]
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
            transcript_hits: list = []

            # Detect "metadata-about-transcripts" queries (e.g. "are there
            # transcripts for X", "do any recordings have transcripts").
            # FTS MATCH on these wordy questions returns junk hits that the
            # LLM then misattributes — better to skip and let the agent
            # call transcript_list_recent / transcript_search itself.
            _meta_transcript_re = re.compile(
                r"\b(?:are\s+there|do\s+(?:any|we\s+have)|is\s+there|which|what|"
                r"any|list|show\s+me|how\s+many)\b[^?]*\btranscripts?\b",
                re.I,
            )
            _is_meta_transcript = bool(_meta_transcript_re.search(prompt))

            # Detect transcript summary/recap requests and resolve by title
            # with fuzzy matching to handle minor misspellings and phrasing.
            def _extract_summary_title(_prompt: str) -> str | None:
                _p = (_prompt or "").strip()
                _pl = _p.lower()

                # Must look like a transcript-summary style ask.
                if "transcript" not in _pl:
                    return None
                if not re.search(r"\b(?:summari[sz]e|summary|recap|what\s+happened)\b", _pl):
                    return None

                # Primary strict pattern
                _m = re.search(
                    r"\b(?:summari[sz]e|summary|recap|what\s+happened)\b[^?]*\btranscript\b[^?]*"
                    r"\b(?:from|for|of)\b\s+(.+?)(?:\?|$)",
                    _p,
                    re.I,
                )
                if _m:
                    _t = _m.group(1).strip().strip('"\' .')
                    return _t or None

                # Fallback: extract text after the last from/for/of token.
                _m2 = re.search(r"\b(?:from|for|of)\b\s+(.+?)(?:\?|$)", _p, re.I)
                if _m2:
                    _t = _m2.group(1).strip().strip('"\' .')
                    return _t or None

                return None

            _summary_title = _extract_summary_title(prompt)
            _has_inline_transcript = bool(
                re.search(r"\btranscript:\s*\S", prompt, re.I)
            )
            if _has_inline_transcript:
                _summary_title = None
            # Summary/recap requests should always go through the dedicated
            # summary path. The meta-transcript regex is intentionally broad
            # for inventory-style questions and can accidentally match long
            # structured prompts that contain words like "list".
            if _summary_title:
                _is_meta_transcript = False
            if _has_inline_transcript:
                _is_meta_transcript = False

            if _has_inline_transcript:
                if status_callback:
                    await status_callback("Analyzing transcript")

                llm_cfg = self.config.get("llm", {})
                summary_params = {
                    "num_predict": int(llm_cfg.get("summary_num_predict", 640)),
                    "temperature": float(llm_cfg.get("summary_temperature", 0.2)),
                }
                summary_messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are a TV episode analyst. Use ONLY the metadata and transcript provided by the user. "
                            "Do not call tools. Do not use prior knowledge. If detail is missing, say 'Not shown in transcript.' "
                            "Return ONLY these sections with headings and bullets: Episode Overview, Plot Breakdown, "
                            "Key Characters, Important Dialogue and Turning Points, Themes and Story Arcs, Key Takeaways."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ]

                llm_result = await self.llm.stream_chat(
                    summary_messages,
                    token_callback=token_callback,
                    params=summary_params,
                )
                response_text = (llm_result or {}).get("response", "")
                if not response_text and llm_result.get("error"):
                    return {"error": llm_result["error"]}
                return {
                    "status": "ok",
                    "llm_response": response_text or "No summary returned.",
                    "transcript_results": [],
                    "fast_path": True,
                    "model": llm_result.get("model", ""),
                }

            async def _call_transcript_tool(_tool: str, _args: Dict[str, Any]) -> Dict[str, Any]:
                import asyncio as _aio
                import json as _json

                _r, _w = await _aio.open_connection("127.0.0.1", 8770, limit=1024 * 1024)
                _w.write((_json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": _tool, "arguments": _args},
                }) + "\n").encode())
                await _w.drain()
                _line = await _aio.wait_for(_r.readline(), timeout=6.0)
                _w.close()
                await _w.wait_closed()
                if not _line:
                    return {}
                _resp = _json.loads(_line.decode())
                _content = _resp.get("result", {}).get("content", [])
                if _content and _content[0].get("type") == "text":
                    _payload = _json.loads(_content[0]["text"])
                    return _payload.get("data", _payload)
                return {}

            if _summary_title:
                if status_callback:
                    await status_callback("Searching transcripts")

                _title_hint = _summary_title

                def _norm_title(_s: str) -> str:
                    return re.sub(r"[^a-z0-9]+", " ", (_s or "").lower()).strip()

                def _pick_best_row(_hint: str, _rows: list[dict]) -> dict | None:
                    import difflib as _difflib

                    _hint_n = _norm_title(_hint)
                    if not _hint_n or not _rows:
                        return None

                    _hint_tokens = set(_hint_n.split())
                    _best = None
                    _best_score = 0.0
                    for _row in _rows:
                        _cand = " ".join([
                            str(_row.get("title") or ""),
                            str(_row.get("episode_title") or _row.get("episode") or ""),
                        ]).strip()
                        _cand_n = _norm_title(_cand)
                        if not _cand_n:
                            continue

                        _cand_tokens = set(_cand_n.split())
                        _token_overlap = len(_hint_tokens & _cand_tokens) / max(1, len(_hint_tokens))
                        _ratio = _difflib.SequenceMatcher(None, _hint_n, _cand_n).ratio()
                        _score = max(_ratio, _token_overlap)
                        if _hint_n in _cand_n:
                            _score = max(_score, 0.95)
                        if _score > _best_score:
                            _best_score = _score
                            _best = _row

                    return _best if _best_score >= 0.45 else None

                try:
                    _rows_data = await _call_transcript_tool(
                        "transcript_cross_search",
                        {"query": _title_hint, "limit": 25},
                    )
                    _rows = _rows_data.get("results") or []
                    if not _rows:
                        _recent_data = await _call_transcript_tool("transcript_list_recent", {"limit": 100})
                        _rows = _recent_data.get("recent") or []

                    _best = _pick_best_row(_title_hint, _rows)
                    if not _best:
                        return {
                            "status": "ok",
                            "llm_response": (
                                f"I couldn't find a close transcript match for '{_title_hint}'. "
                                "Try the exact show title or ask me to list recent transcripts first."
                            ),
                            "transcript_results": _rows[:10],
                            "fast_path": True,
                        }

                    _rid = _best.get("recording_id", "")
                    _summary_data = await _call_transcript_tool(
                        "transcript_recording_summary", {"recording_id": _rid}
                    ) if _rid else {}

                    _summary_obj = _summary_data.get("summary")
                    if isinstance(_summary_obj, dict):
                        _summary_text = _summary_obj.get("summary") or _summary_obj.get("text") or ""
                    elif isinstance(_summary_obj, str):
                        _summary_text = _summary_obj
                    else:
                        _summary_text = ""

                    _title = _best.get("title") or "Unknown"
                    _episode = _best.get("episode_title") or _best.get("episode") or ""
                    _hdr = f'Summary for "{_title}"' + (f' - "{_episode}"' if _episode else "") + ":"

                    if _summary_text:
                        _answer = _hdr + "\n" + _summary_text
                    else:
                        _answer = (
                            _hdr + "\nI found the transcript, but a pre-generated summary is not available yet. "
                            "I can still summarize from transcript excerpts if you want."
                        )

                    return {
                        "status": "ok",
                        "llm_response": _answer,
                        "transcript_results": [_best],
                        "fast_path": True,
                    }
                except Exception as exc:
                    logger.exception("Transcript summary fast-path failed")
                    return {
                        "status": "ok",
                        "llm_response": (
                            "I couldn't access the transcript summary service right now. "
                            "Please try again in a moment. "
                            f"(details: {exc})"
                        ),
                        "transcript_results": [],
                        "fast_path": True,
                    }

            if _is_meta_transcript:
                logger.info("Meta-transcript query — using Python fast-path")
                # Pull a wide list of recent transcripts directly from the
                # transcription service and answer in Python. The 7B model
                # cannot reliably invoke the right tool with the right args
                # for these existence/inventory questions, and any LLM
                # involvement risks fabrication.
                if status_callback:
                    await status_callback("Listing recent transcripts")

                # Resolve user date window from rewritten prompt
                _qd = re.search(
                    r"\((\d{4}-\d{2}-\d{2})(?:\s+to\s+(\d{4}-\d{2}-\d{2}))?\)",
                    prompt,
                )
                _start_ts = _end_ts = None
                _label = ""
                if _qd:
                    try:
                        from datetime import datetime as _dt
                        _s = _dt.strptime(_qd.group(1), "%Y-%m-%d")
                        _e = _dt.strptime(_qd.group(2) or _qd.group(1), "%Y-%m-%d")
                        _start_ts = int(_s.timestamp())
                        _end_ts = int(_e.timestamp()) + 86399
                        _label = (
                            _qd.group(1) if not _qd.group(2)
                            else f"{_qd.group(1)} to {_qd.group(2)}"
                        )
                    except Exception:
                        pass

                # Call transcript_cross_search with date filter (or list_recent
                # as fallback) and format the result deterministically.
                _rows: list = []
                try:
                    import asyncio as _aio, json as _json
                    _r, _w = await _aio.open_connection(
                        "127.0.0.1", 8770, limit=1024 * 1024
                    )
                    if _start_ts is not None:
                        _args = {"date_from": _start_ts, "date_to": _end_ts, "limit": 100}
                        _tool = "transcript_cross_search"
                    else:
                        _args = {"limit": 50}
                        _tool = "transcript_list_recent"
                    _w.write((_json.dumps({
                        "jsonrpc": "2.0", "id": 1,
                        "method": "tools/call",
                        "params": {"name": _tool, "arguments": _args},
                    }) + "\n").encode())
                    await _w.drain()
                    _line = await _aio.wait_for(_r.readline(), timeout=5.0)
                    _w.close(); await _w.wait_closed()
                    if _line:
                        _resp = _json.loads(_line.decode())
                        _content = _resp.get("result", {}).get("content", [])
                        if _content and _content[0].get("type") == "text":
                            _payload = _json.loads(_content[0]["text"])
                            _data = _payload.get("data", _payload)
                            _rows = _data.get("results") or _data.get("recent") or []
                except Exception:
                    logger.exception("Meta-transcript fast-path query failed")

                # ── Parse Channels DVR filename into clean components ──
                import re as _re2
                _cdvr_pat = _re2.compile(
                    r'^(.+?)\s+(S\d{2}E\d{2})\s+(.+?)\s+\d{4}-\d{2}-\d{2}-\d{4}$'
                )

                def _parse_title(raw: str):
                    """Parse 'Show S01E02 Episode Title 2026-04-28-1900' → (show, ep_title, se)."""
                    m = _cdvr_pat.match(raw)
                    if m:
                        return m.group(1), m.group(3), m.group(2)
                    # Fallback: strip trailing date
                    cleaned = _re2.sub(r'\s+\d{4}-\d{2}-\d{2}-\d{4}$', '', raw)
                    return cleaned, "", ""

                # Build a clean Python-authored answer
                from datetime import datetime as _dt2
                _seen: set[str] = set()
                _items: list[str] = []
                _enriched: list[dict] = []
                for _r2 in _rows:
                    _t = _r2.get("title") or "Unknown"
                    _e = _r2.get("episode_title") or _r2.get("episode") or ""
                    _rd = _r2.get("record_date") or _r2.get("air_date") or _r2.get("created_at")
                    _ds = ""
                    try:
                        _ds = _dt2.fromtimestamp(float(_rd)).strftime("%Y-%m-%d") if _rd else ""
                    except Exception:
                        _ds = ""

                    # Parse show name and episode from Channels DVR filename
                    _show, _ep_parsed, _se = _parse_title(_t)
                    if not _e:
                        _e = _ep_parsed

                    _key = f"{_show}|{_e}|{_ds}"
                    if _key in _seen:
                        continue
                    _seen.add(_key)

                    # Format like the LLM does: "Show" "Episode" S##E##
                    if _e and _se:
                        _line2 = f'- "{_show}" "{_e}" {_se}'
                    elif _e:
                        _line2 = f'- "{_show}" "{_e}"'
                    elif _se:
                        _line2 = f'- "{_show}" {_se}'
                    else:
                        _line2 = f'- "{_show}"'
                    if _ds:
                        _line2 += f" ({_ds})"
                    _items.append(_line2)

                    # Enrich row for episode cards
                    _enriched.append({
                        **_r2,
                        "display_title": _show,
                        "episode_title": _e or None,
                        "se_label": _se or None,
                    })

                if _items:
                    if _label:
                        _ans = (
                            f"Yes — transcripts are available for {len(_items)} "
                            f"recording(s) from {_label}:\n" + "\n".join(_items)
                        )
                    else:
                        _ans = (
                            f"There are transcripts available for {len(_items)} "
                            f"recent recording(s):\n" + "\n".join(_items)
                        )
                else:
                    if _label:
                        _ans = f"No transcripts are available for recordings from {_label}."
                    else:
                        _ans = "No transcripts are available."

                return {
                    "status": "ok",
                    "llm_response": _ans,
                    "transcript_results": _enriched[:25] if _enriched else _rows[:25],
                    "fast_path": True,
                }

            # ── FTS pre-fetch path (date-scoped content search) ──
            transcript_context = ""
            transcript_hits = []
            if temporal not in ("future", "present"):
                try:
                    if status_callback:
                        await status_callback("Searching transcripts")

                    # Extract resolved date(s) from the rewritten prompt so we
                    # filter transcripts to the user's date window. Without this
                    # filter, fuzzy text matches return random shows that the
                    # LLM may falsely attribute to "last night".
                    _date_filters: Dict[str, Any] = {}
                    _date_label = ""
                    _qd = re.search(
                        r"\((\d{4}-\d{2}-\d{2})(?:\s+to\s+(\d{4}-\d{2}-\d{2}))?\)",
                        prompt,
                    )
                    if _qd:
                        try:
                            from datetime import datetime as _dt
                            _start = _dt.strptime(_qd.group(1), "%Y-%m-%d")
                            _end_str = _qd.group(2) or _qd.group(1)
                            _end = _dt.strptime(_end_str, "%Y-%m-%d")
                            # Inclusive end-of-day
                            _date_filters["date_from"] = int(_start.timestamp())
                            _date_filters["date_to"] = int(_end.timestamp()) + 86399
                            _date_label = (
                                _qd.group(1) if not _qd.group(2)
                                else f"{_qd.group(1)} to {_qd.group(2)}"
                            )
                        except Exception:
                            _date_filters = {}

                    transcript_results = await self.search.transcript_search(
                        prompt, filters=_date_filters or None
                    )
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
                            # Include record date so the LLM can verify date claims
                            rec_date = r.get("record_date") or r.get("air_date")
                            date_str = ""
                            if rec_date:
                                try:
                                    from datetime import datetime as _dt2
                                    date_str = " on " + _dt2.fromtimestamp(int(rec_date)).strftime("%Y-%m-%d")
                                except Exception:
                                    pass
                            if ep:
                                lines.append(f'From "{title}" - "{ep}"{date_str} at {time_str}: {snippet}')
                            else:
                                lines.append(f'From "{title}"{date_str} at {time_str}: {snippet}')
                        transcript_context = "\n".join(lines)
                    elif _date_label:
                        # Negative result is informative — surface it so the LLM
                        # doesn't hallucinate transcripts for the requested date.
                        transcript_context = (
                            f"No transcripts found for recordings on {_date_label}."
                        )
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

            # Run the selected planner (AgentLoop by default).
            primary_name = self._resolve_planner_name(metadata)
            planner = self._get_planner(metadata)
            agent_result = await planner.run(
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

            # Optional shadow planner execution for evaluation.
            shadow_info: Dict[str, Any] | None = None
            shadow_name = self._resolve_shadow_planner_name(metadata)
            if shadow_name and shadow_name != primary_name:
                try:
                    shadow_planner = self._planner_registry.get(shadow_name)
                    shadow_result = await shadow_planner.run(
                        prompt,
                        transcript_context=transcript_context,
                        semantic_context=semantic_context,
                        systems=systems,
                        temporal=temporal,
                        domains=domains,
                        entity_store=self.entity_store,
                        status_callback=None,
                        token_callback=None,
                    )
                    shadow_info = {
                        "planner": shadow_name,
                        "status": shadow_result.get("status", "unknown"),
                        "iterations": shadow_result.get("iterations", 0),
                    }
                except Exception as exc:
                    logger.warning("Shadow planner '%s' failed: %s", shadow_name, exc)
                    shadow_info = {
                        "planner": shadow_name,
                        "status": "error",
                        "error": str(exc),
                    }

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
            if shadow_info is not None:
                llm_result["shadow"] = shadow_info

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
