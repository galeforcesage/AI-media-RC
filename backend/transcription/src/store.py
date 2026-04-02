"""
store.py
SQLite-backed metadata store with FTS5 full-text search.

Stores transcripts, summaries, keywords, and supports natural-language search.
"""

from __future__ import annotations
import json
import logging
import sqlite3
import time
from typing import Dict, List, Optional

from .models import TranscriptMetadata

logger = logging.getLogger(__name__)


class MetadataStore:
    """Persistent transcript metadata with full-text search."""

    def __init__(self, db_path: str = "transcription.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                recording_id TEXT PRIMARY KEY,
                system TEXT NOT NULL,
                title TEXT DEFAULT '',
                episode TEXT DEFAULT '',
                duration REAL DEFAULT 0,
                word_count INTEGER DEFAULT 0,
                transcript TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                keywords TEXT DEFAULT '[]',
                topics TEXT DEFAULT '[]',
                scenes TEXT DEFAULT '[]',
                vtt TEXT DEFAULT '',
                created_at REAL
            )
        """)

        # FTS5 virtual table for full-text search
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
                recording_id,
                title,
                episode,
                transcript,
                summary,
                keywords,
                content='transcripts',
                content_rowid='rowid'
            )
        """)

        # Triggers to keep FTS in sync
        self._conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS transcripts_ai AFTER INSERT ON transcripts BEGIN
                INSERT INTO transcripts_fts(recording_id, title, episode, transcript, summary, keywords)
                VALUES (new.recording_id, new.title, new.episode, new.transcript, new.summary, new.keywords);
            END;
            CREATE TRIGGER IF NOT EXISTS transcripts_ad AFTER DELETE ON transcripts BEGIN
                INSERT INTO transcripts_fts(transcripts_fts, recording_id, title, episode, transcript, summary, keywords)
                VALUES ('delete', old.recording_id, old.title, old.episode, old.transcript, old.summary, old.keywords);
            END;
            CREATE TRIGGER IF NOT EXISTS transcripts_au AFTER UPDATE ON transcripts BEGIN
                INSERT INTO transcripts_fts(transcripts_fts, recording_id, title, episode, transcript, summary, keywords)
                VALUES ('delete', old.recording_id, old.title, old.episode, old.transcript, old.summary, old.keywords);
                INSERT INTO transcripts_fts(recording_id, title, episode, transcript, summary, keywords)
                VALUES (new.recording_id, new.title, new.episode, new.transcript, new.summary, new.keywords);
            END;
        """)

        self._conn.commit()
        logger.info("Metadata store opened: %s", self.db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def save(self, meta: TranscriptMetadata) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO transcripts
               (recording_id, system, title, episode, duration, word_count,
                transcript, summary, keywords, topics, scenes, vtt, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (meta.recording_id, meta.system, meta.title, meta.episode,
             meta.duration, meta.word_count, meta.transcript, meta.summary,
             json.dumps(meta.keywords), json.dumps(meta.topics),
             json.dumps(meta.scenes), meta.vtt, meta.created_at),
        )
        self._conn.commit()
        logger.info("Saved transcript for %s (%s)", meta.recording_id, meta.title)

    def get(self, recording_id: str) -> Optional[TranscriptMetadata]:
        row = self._conn.execute(
            "SELECT * FROM transcripts WHERE recording_id = ?", (recording_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_meta(row)

    def search(self, query: str, limit: int = 20) -> List[TranscriptMetadata]:
        rows = self._conn.execute(
            """SELECT t.* FROM transcripts t
               INNER JOIN transcripts_fts f ON t.recording_id = f.recording_id
               WHERE transcripts_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [self._row_to_meta(r) for r in rows]

    def list_recent(self, limit: int = 50) -> List[TranscriptMetadata]:
        rows = self._conn.execute(
            "SELECT * FROM transcripts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_meta(r) for r in rows]

    def stats(self) -> Dict:
        row = self._conn.execute(
            "SELECT COUNT(*) as total, COALESCE(SUM(word_count), 0) as words, "
            "COALESCE(SUM(duration), 0) as duration FROM transcripts"
        ).fetchone()
        return {
            "total_transcripts": row["total"],
            "total_words": row["words"],
            "total_duration_hours": round(row["duration"] / 3600, 1),
        }

    def _row_to_meta(self, row) -> TranscriptMetadata:
        return TranscriptMetadata(
            recording_id=row["recording_id"],
            system=row["system"],
            title=row["title"],
            episode=row["episode"],
            duration=row["duration"],
            word_count=row["word_count"],
            transcript=row["transcript"],
            summary=row["summary"],
            keywords=json.loads(row["keywords"]) if row["keywords"] else [],
            topics=json.loads(row["topics"]) if row["topics"] else [],
            scenes=json.loads(row["scenes"]) if row["scenes"] else [],
            vtt=row["vtt"],
            created_at=row["created_at"],
        )
