"""
transcription_query.py
Query object for the transcription pipeline.
Encapsulates audio source, options, and result tracking.
"""

from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TranscriptionQuery:
    """
    Represents a single transcription request flowing through the pipeline.

    Tracks the audio source, processing options, timing, and result.
    """

    audio_path: str
    language: Optional[str] = None
    model_size: str = "base"
    prompt: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)

    query_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    text: Optional[str] = None
    segments: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        """Whether the transcription has finished (success or error)."""
        return self.completed_at is not None

    @property
    def elapsed(self) -> Optional[float]:
        """Seconds elapsed from creation to completion, or None if still pending."""
        if self.completed_at is None:
            return None
        return self.completed_at - self.created_at

    def complete(self, text: str, segments: List[Dict[str, Any]] | None = None) -> None:
        """Mark the query as successfully completed."""
        self.text = text
        self.segments = segments or []
        self.completed_at = time.time()

    def fail(self, error: str) -> None:
        """Mark the query as failed."""
        self.error = error
        self.completed_at = time.time()

    def validate(self) -> List[str]:
        """Return a list of validation errors (empty if valid)."""
        errors: List[str] = []
        if not self.audio_path:
            errors.append("audio_path is required")
        if self.model_size not in {"tiny", "base", "small", "medium", "large"}:
            errors.append(f"invalid model_size '{self.model_size}'")
        return errors

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dictionary."""
        return {
            "query_id": self.query_id,
            "audio_path": self.audio_path,
            "language": self.language,
            "model_size": self.model_size,
            "prompt": self.prompt,
            "options": self.options,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "text": self.text,
            "segments": self.segments,
            "error": self.error,
        }
