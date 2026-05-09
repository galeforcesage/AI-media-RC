"""
models.py
Data models for the transcription subsystem.
"""

from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class TranscriptionJob:
    job_id: str = ""
    system: str = ""  # sagetv | channelsdvr
    recording_id: str = ""
    file_path: str = ""
    temp_audio_path: str = ""
    status: str = "pending"  # pending, extracting, processing, done, error
    attempts: int = 0
    max_attempts: int = 3
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    duration: float = 0.0

    def __post_init__(self):
        if not self.job_id:
            self.job_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.time()
        self.updated_at = self.created_at

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TranscriptMetadata:
    recording_id: str = ""
    system: str = ""
    title: str = ""
    episode: str = ""
    duration: float = 0.0
    word_count: int = 0
    transcript: str = ""
    summary: str = ""
    keywords: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    scenes: List[Dict] = field(default_factory=list)
    vtt: str = ""
    source: str = "stt"  # 'stt' | 'cc' | 'mixed' — how the transcript was produced
    created_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
