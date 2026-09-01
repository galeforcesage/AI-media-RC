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

        # Additive migration: source column tracks how the transcript was produced
        # ('stt' = full whisper, 'cc' = closed captions only, 'mixed' = cc+stt gap-fill).
        # Existing rows default to 'stt' which is accurate for everything created before
        # the CC pipeline landed.
        try:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(transcripts)").fetchall()}
            if "source" not in cols:
                self._conn.execute("ALTER TABLE transcripts ADD COLUMN source TEXT DEFAULT 'stt'")
                logger.info("transcripts: added 'source' column (default 'stt')")
        except Exception:
            logger.exception("Failed to add 'source' column (non-fatal)")

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
        self._check_fts_integrity()
        logger.info("Metadata store opened: %s", self.db_path)

    def _check_fts_integrity(self) -> None:
        """Verify FTS5 index is usable; rebuild from content table if corrupt."""
        try:
            self._conn.execute(
                "SELECT count(*) FROM transcripts_fts WHERE transcripts_fts MATCH 'test'"
            ).fetchone()
        except Exception as e:
            logger.warning("FTS5 index corrupt (%s), rebuilding...", e)
            try:
                self._conn.execute(
                    "INSERT INTO transcripts_fts(transcripts_fts) VALUES('rebuild')"
                )
                self._conn.commit()
                logger.info("FTS5 index rebuilt successfully")
            except Exception:
                logger.warning("FTS5 rebuild failed, dropping and recreating...")
                self._conn.executescript("""
                    DROP TABLE IF EXISTS transcripts_fts;
                    CREATE VIRTUAL TABLE transcripts_fts USING fts5(
                        recording_id, title, episode, transcript, summary, keywords,
                        content='transcripts', content_rowid='rowid'
                    );
                    INSERT INTO transcripts_fts(transcripts_fts) VALUES('rebuild');
                """)
                self._conn.commit()
                logger.info("FTS5 index recreated and rebuilt from content table")

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def save(self, meta: TranscriptMetadata, append: bool = False) -> None:
        if append:
            # Append transcript text to existing record for incremental transcription
            existing = self._conn.execute(
                "SELECT recording_id, transcript, word_count, duration FROM transcripts WHERE recording_id = ?",
                (meta.recording_id,),
            ).fetchone()
            if existing:
                combined_text = (existing["transcript"] or "") + "\n" + meta.transcript
                combined_words = existing["word_count"] + meta.word_count
                combined_duration = max(existing["duration"], meta.duration)
                self._conn.execute(
                    "UPDATE transcripts SET transcript = ?, word_count = ?, duration = ?, "
                    "vtt = vtt || ?, created_at = ? WHERE recording_id = ?",
                    (combined_text, combined_words, combined_duration,
                     "\n" + meta.vtt if meta.vtt else "", time.time(),
                     meta.recording_id),
                )
                self._conn.commit()
                logger.info("Appended transcript for %s (now %d words)",
                            meta.recording_id, combined_words)
                return
            # No existing record — fall through to insert
        self._conn.execute(
            """INSERT OR REPLACE INTO transcripts
               (recording_id, system, title, episode, duration, word_count,
                transcript, summary, keywords, topics, scenes, vtt, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (meta.recording_id, meta.system, meta.title, meta.episode,
             meta.duration, meta.word_count, meta.transcript, meta.summary,
             json.dumps(meta.keywords), json.dumps(meta.topics),
             json.dumps(meta.scenes), meta.vtt,
             getattr(meta, "source", "stt") or "stt",
             meta.created_at),
        )
        self._conn.commit()
        logger.info("Saved transcript for %s (%s) [source=%s]",
                    meta.recording_id, meta.title, getattr(meta, "source", "stt"))

    def get(self, recording_id: str) -> Optional[TranscriptMetadata]:
        row = self._conn.execute(
            "SELECT * FROM transcripts WHERE recording_id = ?", (recording_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_meta(row)

    def delete(self, recording_id: str) -> bool:
        """Delete a transcript by recording_id. Returns True if a row was removed."""
        cursor = self._conn.execute(
            "DELETE FROM transcripts WHERE recording_id = ?", (recording_id,)
        )
        self._conn.commit()
        if cursor.rowcount > 0:
            logger.info("Deleted transcript for %s", recording_id)
            return True
        return False

    def search(self, query: str, limit: int = 20) -> List[TranscriptMetadata]:
        # FTS5's default tokenizer breaks on punctuation (`&`, `'`, `:`, etc.) and
        # treats unquoted bare words as a special syntax. Sanitize the user query:
        # split into alphanumeric terms, drop empties, then quote each term so it's
        # treated as a literal (case-insensitive prefix search via the *) and AND'd
        # together. This way `"Georgie & Mandy's First Marriage"` becomes
        # `"georgie" AND "mandy" AND "first" AND "marriage"`.
        import re
        terms = [t for t in re.findall(r"[A-Za-z0-9]+", query or "") if t]
        if not terms:
            return []
        # Try strict AND first, then fall back to OR so multi-word queries
        # that share no single chunk still return the best-ranked matches.
        and_query = " AND ".join(f'"{t}"' for t in terms)
        or_query = " OR ".join(f'"{t}"' for t in terms)
        _sql = """SELECT t.* FROM transcripts t
                   INNER JOIN transcripts_fts f ON t.recording_id = f.recording_id
                   WHERE transcripts_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?"""
        try:
            rows = self._conn.execute(_sql, (and_query, limit)).fetchall()
            if not rows and or_query != and_query:
                rows = self._conn.execute(_sql, (or_query, limit)).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("FTS search failed for %r (sanitized=%r): %s", query, and_query, exc)
            return []
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
        named = self._conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE recording_id NOT LIKE 'stream%'"
        ).fetchone()[0]
        diarized = self._conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE vtt LIKE '%<v %' AND recording_id NOT LIKE 'stream%'"
        ).fetchone()[0]
        # Source breakdown (CC vs STT vs mixed). Defaults to 'stt' for legacy rows.
        source_counts = {"cc": 0, "stt": 0, "mixed": 0}
        try:
            for r in self._conn.execute(
                "SELECT COALESCE(source, 'stt') as src, COUNT(*) as c "
                "FROM transcripts WHERE recording_id NOT LIKE 'stream%' GROUP BY src"
            ).fetchall():
                source_counts[r["src"]] = r["c"]
        except Exception:
            pass
        return {
            "total_transcripts": row["total"],
            "total_words": row["words"],
            "total_duration_hours": round(row["duration"] / 3600, 1),
            "named_shows": named,
            "diarized_shows": diarized,
            "cc_only_count": source_counts.get("cc", 0),
            "stt_full_count": source_counts.get("stt", 0),
            "mixed_count": source_counts.get("mixed", 0),
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
            source=(row["source"] if "source" in row.keys() and row["source"] else "stt"),
            created_at=row["created_at"],
        )
