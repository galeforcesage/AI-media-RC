"""
voice_session.py
Session manager for multi-turn voice interactions.
Maintains conversation history, context state, and active tool tracking.
Integrates with LLMPipeline for end-to-end processing.
"""

from __future__ import annotations
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.llm_pipeline import LLMPipeline
from services.llm import LLMService

logger = logging.getLogger(__name__)


@dataclass
class ConversationTurn:
    """A single turn in a voice conversation."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class VoiceSessionData:
    """State container for one voice session."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    history: List[ConversationTurn] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    active_tools: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = time.time()


class VoiceSessionManager:
    """
    Manages voice interaction sessions with conversation history,
    multi-turn LLM context, and active tool state.
    """

    def __init__(
        self,
        pipeline: LLMPipeline,
        llm: LLMService,
        max_history: int = 20,
        session_ttl: float = 1800.0,
    ) -> None:
        self.pipeline = pipeline
        self.llm = llm
        self.max_history = max_history
        self.session_ttl = session_ttl
        self._sessions: Dict[str, VoiceSessionData] = {}

    def create_session(self, metadata: Dict[str, Any] | None = None) -> VoiceSessionData:
        """Create a new voice session."""
        session = VoiceSessionData(metadata=metadata or {})
        self._sessions[session.session_id] = session
        logger.info("Session created: %s", session.session_id)
        return session

    def get_session(self, session_id: str) -> VoiceSessionData | None:
        """Retrieve an existing session by ID. Returns None if expired."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session.last_activity > self.session_ttl:
            logger.info("Session expired: %s", session_id)
            self._sessions.pop(session_id, None)
            return None
        return session

    def close_session(self, session_id: str) -> bool:
        """Remove a session. Returns True if the session existed."""
        removed = self._sessions.pop(session_id, None) is not None
        if removed:
            logger.info("Session closed: %s", session_id)
        return removed

    def list_sessions(self) -> List[str]:
        """Return IDs of all active (non-expired) sessions."""
        now = time.time()
        return [
            sid for sid, s in self._sessions.items()
            if now - s.last_activity <= self.session_ttl
        ]

    def _build_prompt(self, session: VoiceSessionData, user_text: str) -> str:
        """Build a contextual prompt from conversation history."""
        lines: List[str] = []
        for turn in session.history[-self.max_history:]:
            prefix = "User" if turn.role == "user" else "Assistant"
            lines.append(f"{prefix}: {turn.content}")
        lines.append(f"User: {user_text}")
        lines.append("Assistant:")
        return "\n".join(lines)

    async def handle_voice(self, session_id: str, audio_path: str) -> Dict[str, Any]:
        """
        Process a voice input within an existing session.
        Transcribes audio, generates a contextual LLM response, and synthesizes TTS.
        """
        session = self.get_session(session_id)
        if session is None:
            return {"error": f"Session '{session_id}' not found or expired"}

        logger.info("Voice input for session %s: %s", session_id, audio_path)
        transcription = await self.pipeline.whisper.transcribe(audio_path)
        if transcription.get("status") != "ok":
            logger.error("Transcription failed in session %s", session_id)
            return {"error": "Transcription failed", "detail": transcription}

        user_text = transcription["text"]
        return await self._generate_turn(session, user_text)

    async def handle_text(self, session_id: str, text: str) -> Dict[str, Any]:
        """
        Process a text input within an existing session.
        Generates a contextual LLM response and synthesizes TTS.
        """
        session = self.get_session(session_id)
        if session is None:
            return {"error": f"Session '{session_id}' not found or expired"}

        logger.info("Text input for session %s", session_id)
        return await self._generate_turn(session, text)

    async def _generate_turn(
        self,
        session: VoiceSessionData,
        user_text: str,
    ) -> Dict[str, Any]:
        """Run one conversation turn: append user input, call LLM, append response."""
        session.touch()
        session.history.append(ConversationTurn(role="user", content=user_text))

        prompt = self._build_prompt(session, user_text)

        try:
            llm_result = await self.llm.generate(prompt)
        except Exception as exc:
            logger.exception("LLM generation error in session %s", session.session_id)
            return {"error": f"LLM generation failed: {exc}"}

        if llm_result.get("status") != "ok":
            return {"error": "LLM generation failed", "detail": llm_result}

        response_text = llm_result["response"]
        session.history.append(ConversationTurn(role="assistant", content=response_text))

        tts_result = await self.pipeline.tts.synthesize(response_text)
        audio_path = tts_result.get("audio_path") if tts_result.get("status") == "ok" else None

        logger.info("Turn complete for session %s", session.session_id)
        return {
            "status": "ok",
            "session_id": session.session_id,
            "transcription": user_text,
            "llm_response": response_text,
            "audio_path": audio_path,
        }

    async def cleanup_expired(self) -> int:
        """Remove sessions that have exceeded their TTL. Returns count removed."""
        now = time.time()
        expired = [
            sid
            for sid, s in self._sessions.items()
            if now - s.last_activity > self.session_ttl
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
        if expired:
            logger.info("Cleaned up %d expired sessions", len(expired))
        return len(expired)
