"""
conversation_memory.py
Per-session short-term conversation memory.

Remembers the last few (question, answer, episodes) turns for a session so
follow-up questions ("what did the other team win?", "did that one finish?")
have the context they need. This is a lightweight in-memory ring buffer, not a
tool the LLM calls -- the orchestrator injects a compact summary into the
planner context and records each answer automatically.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional


def _norm_recordings(recordings) -> List[dict]:
    recs: List[dict] = []
    for r in (recordings or []):
        if not r:
            continue
        rid = r.get("id") or r.get("recording_id") or ""
        recs.append({
            "id": rid,
            "title": (r.get("title") or "").strip(),
            "episode_title": (r.get("episode_title") or "").strip(),
            "record_date": r.get("record_date"),
        })
        if len(recs) >= 5:
            break
    return recs


def _label(recs: List[dict]) -> str:
    parts = []
    for r in recs:
        t = r.get("title") or "Unknown"
        ep = r.get("episode_title") or ""
        parts.append(f"{t} - {ep}" if ep else t)
    return "; ".join(parts)


class ConversationMemory:
    """A per-session ring buffer of recent Q&A turns."""

    def __init__(self, max_turns: int = 4, ttl: float = 1800.0):
        self._max_turns = max(1, int(max_turns))
        self._ttl = float(ttl)
        self._sessions: Dict[str, Deque[dict]] = {}
        self._lock = Lock()

    @staticmethod
    def _key(session_id: Optional[str]) -> str:
        sid = (session_id or "").strip()
        return sid or "default"

    def add_turn(
        self,
        session_id: Optional[str],
        question: str,
        answer: str,
        recordings: Optional[List[dict]] = None,
    ) -> None:
        q = (question or "").strip()
        a = (answer or "").strip()
        if not q and not a:
            return
        with self._lock:
            key = self._key(session_id)
            dq = self._sessions.get(key)
            if dq is None:
                dq = deque(maxlen=self._max_turns)
                self._sessions[key] = dq
            dq.append({
                "ts": time.time(),
                "question": q[:400],
                "answer": a[:600],
                "recordings": _norm_recordings(recordings),
            })

    def _live_turns(self, session_id: Optional[str]) -> List[dict]:
        now = time.time()
        with self._lock:
            key = self._key(session_id)
            dq = self._sessions.get(key)
            if not dq:
                return []
            turns = [t for t in dq if now - t["ts"] <= self._ttl]
            self._sessions[key] = deque(turns, maxlen=self._max_turns)
            return list(turns)

    def last_recordings(self, session_id: Optional[str]) -> List[dict]:
        """Recordings from the most recent turn that referenced any."""
        for t in reversed(self._live_turns(session_id)):
            if t.get("recordings"):
                return list(t["recordings"])
        return []

    def format_for_prompt(self, session_id: Optional[str]) -> str:
        """Return a compact context block, or empty string if nothing recent."""
        turns = self._live_turns(session_id)
        if not turns:
            return ""
        lines = [
            "EARLIER IN THIS CONVERSATION (oldest first). Use this only to "
            "resolve follow-up references such as \"that episode\", \"the other "
            "one\", \"what did they win\". Do NOT re-answer these earlier "
            "questions; treat them as background:",
        ]
        for i, t in enumerate(turns, 1):
            eps = _label(t.get("recordings") or [])
            ep_str = f" [about: {eps}]" if eps else ""
            lines.append(f"{i}. Q: {t['question']}")
            lines.append(f"   A{ep_str}: {t['answer']}")
        return "\n".join(lines) + "\n"