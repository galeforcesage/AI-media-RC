"""
transcript_index.py
SQLite-backed transcript index with FTS5 for cross-metadata reasoning.

Manages the recordings, actors, transcript_chunks, and FTS5 tables
defined in Appendix X.
"""

from __future__ import annotations
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TranscriptIndex:
    """Cross-metadata transcript index with full-text search."""

    def __init__(self, db_path: str = "transcript_index.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._open()
        self._create_tables()

    def _open(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        logger.info("Transcript index opened: %s", self.db_path)

    @contextmanager
    def _transaction(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS recordings (
                recording_id    TEXT PRIMARY KEY,
                system          TEXT NOT NULL CHECK (system IN ('sagetv', 'channelsdvr')),
                title           TEXT NOT NULL,
                episode_title   TEXT,
                season          INTEGER,
                episode         INTEGER,
                genre           TEXT,
                channel         TEXT,
                channel_number  TEXT,
                air_date        INTEGER,
                record_date     INTEGER,
                duration        REAL,
                file_path       TEXT,
                file_size       INTEGER,
                description     TEXT,
                rating          TEXT,
                source_id       TEXT,
                sidecar_path    TEXT,
                transcribed_at  INTEGER,
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_recordings_system ON recordings(system);
            CREATE INDEX IF NOT EXISTS idx_recordings_title ON recordings(title);
            CREATE INDEX IF NOT EXISTS idx_recordings_genre ON recordings(genre);
            CREATE INDEX IF NOT EXISTS idx_recordings_channel ON recordings(channel);
            CREATE INDEX IF NOT EXISTS idx_recordings_record_date ON recordings(record_date);
            CREATE INDEX IF NOT EXISTS idx_recordings_air_date ON recordings(air_date);

            CREATE TABLE IF NOT EXISTS actors (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id    TEXT NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
                actor_name      TEXT NOT NULL,
                role            TEXT,
                billing_order   INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_actors_recording ON actors(recording_id);
            CREATE INDEX IF NOT EXISTS idx_actors_name ON actors(actor_name COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS transcript_chunks (
                chunk_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id    TEXT NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
                chunk_index     INTEGER NOT NULL,
                start_time      REAL NOT NULL,
                end_time        REAL NOT NULL,
                text            TEXT NOT NULL,
                speaker         TEXT,
                confidence      REAL,
                word_count      INTEGER,
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_recording ON transcript_chunks(recording_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_time ON transcript_chunks(recording_id, start_time);

            CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
                text,
                content='transcript_chunks',
                content_rowid='chunk_id',
                tokenize='porter unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS transcript_chunks_ai AFTER INSERT ON transcript_chunks BEGIN
                INSERT INTO transcript_fts(rowid, text) VALUES (new.chunk_id, new.text);
            END;

            CREATE TRIGGER IF NOT EXISTS transcript_chunks_ad AFTER DELETE ON transcript_chunks BEGIN
                INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
            END;

            CREATE TRIGGER IF NOT EXISTS transcript_chunks_au AFTER UPDATE ON transcript_chunks BEGIN
                INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
                INSERT INTO transcript_fts(rowid, text) VALUES (new.chunk_id, new.text);
            END;

            CREATE TABLE IF NOT EXISTS transcript_summaries (
                recording_id    TEXT PRIMARY KEY REFERENCES recordings(recording_id) ON DELETE CASCADE,
                summary         TEXT NOT NULL,
                keywords        TEXT,
                topics          TEXT,
                generated_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
        """)
        self._conn.commit()
        logger.info("Transcript index tables created")

    # ------------------------------------------------------------------
    # Insert operations
    # ------------------------------------------------------------------

    def insert_recording(self, recording: Dict[str, Any]) -> None:
        with self._transaction():
            self._conn.execute(
                """INSERT OR REPLACE INTO recordings
                   (recording_id, system, title, episode_title, season, episode,
                    genre, channel, channel_number, air_date, record_date,
                    duration, file_path, file_size, description, rating,
                    source_id, sidecar_path, transcribed_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           strftime('%s', 'now'))""",
                (
                    recording["recording_id"],
                    recording["system"],
                    recording["title"],
                    recording.get("episode_title"),
                    recording.get("season"),
                    recording.get("episode"),
                    recording.get("genre"),
                    recording.get("channel"),
                    recording.get("channel_number"),
                    recording.get("air_date"),
                    recording.get("record_date"),
                    recording.get("duration"),
                    recording.get("file_path"),
                    recording.get("file_size"),
                    recording.get("description"),
                    recording.get("rating"),
                    recording.get("source_id"),
                    recording.get("sidecar_path"),
                    recording.get("transcribed_at"),
                ),
            )
        logger.info("Indexed recording %s", recording["recording_id"])

    def insert_actors(self, recording_id: str, actors: List[Dict[str, Any]]) -> None:
        with self._transaction():
            self._conn.execute(
                "DELETE FROM actors WHERE recording_id = ?", (recording_id,)
            )
            for actor in actors:
                self._conn.execute(
                    """INSERT INTO actors (recording_id, actor_name, role, billing_order)
                       VALUES (?, ?, ?, ?)""",
                    (
                        recording_id,
                        actor["name"],
                        actor.get("role"),
                        actor.get("billing_order"),
                    ),
                )
        logger.info("Indexed %d actors for %s", len(actors), recording_id)

    def insert_chunks(self, recording_id: str, chunks: List[Dict[str, Any]]) -> None:
        with self._transaction():
            self._conn.execute(
                "DELETE FROM transcript_chunks WHERE recording_id = ?", (recording_id,)
            )
            for chunk in chunks:
                self._conn.execute(
                    """INSERT INTO transcript_chunks
                       (recording_id, chunk_index, start_time, end_time, text,
                        speaker, confidence, word_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        recording_id,
                        chunk["index"],
                        chunk["start_time"],
                        chunk["end_time"],
                        chunk["text"],
                        chunk.get("speaker"),
                        chunk.get("confidence"),
                        chunk.get("word_count", len(chunk["text"].split())),
                    ),
                )
        logger.info("Indexed %d chunks for %s", len(chunks), recording_id)

    def insert_summary(self, recording_id: str, summary: Dict[str, Any]) -> None:
        with self._transaction():
            self._conn.execute(
                """INSERT OR REPLACE INTO transcript_summaries
                   (recording_id, summary, keywords, topics)
                   VALUES (?, ?, ?, ?)""",
                (
                    recording_id,
                    summary.get("text", ""),
                    json.dumps(summary.get("keywords", [])),
                    json.dumps(summary.get("topics", [])),
                ),
            )
        logger.info("Saved summary for %s", recording_id)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_recording(self, recording_id: str) -> None:
        with self._transaction():
            self._conn.execute(
                "DELETE FROM recordings WHERE recording_id = ?", (recording_id,)
            )
        logger.info("Deleted recording %s from index", recording_id)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_recording(self, recording_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM recordings WHERE recording_id = ?", (recording_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _coerce_date_to_epoch(value: Any, end_of_day: bool = False) -> Optional[int]:
        """Accept Unix epoch (int/str) or ISO YYYY-MM-DD; return epoch seconds."""
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
        try:
            from datetime import datetime as _dt
            s = str(value).strip()
            # Accept "YYYY-MM-DD" or full ISO
            if len(s) == 10 and s[4] == "-" and s[7] == "-":
                d = _dt.strptime(s, "%Y-%m-%d")
            else:
                d = _dt.fromisoformat(s.replace("Z", "+00:00"))
            ts = int(d.timestamp())
            if end_of_day:
                ts += 86399
            return ts
        except Exception:
            logger.warning("Could not parse date filter: %r", value)
            return None

    def list_recordings(
        self,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List recordings (with transcripts) matching metadata filters.

        Used when the caller wants ``transcript_cross_search`` semantics
        but has no full-text query — e.g. "list transcripts recorded
        between X and Y".
        """
        filters = filters or {}
        conditions: list[str] = []
        params: list = []
        if filters.get("actor"):
            conditions.append(
                "recording_id IN (SELECT recording_id FROM actors WHERE actor_name LIKE ?)"
            )
            params.append(f"%{filters['actor']}%")
        if filters.get("genre"):
            conditions.append("genre LIKE ?")
            params.append(f"%{filters['genre']}%")
        if filters.get("channel"):
            conditions.append("(channel = ? OR channel_number = ?)")
            params.extend([filters["channel"], filters["channel"]])
        _df = self._coerce_date_to_epoch(filters.get("date_from"))
        if _df is not None:
            conditions.append("record_date >= ?")
            params.append(_df)
        _dt2 = self._coerce_date_to_epoch(filters.get("date_to"), end_of_day=True)
        if _dt2 is not None:
            conditions.append("record_date <= ?")
            params.append(_dt2)
        if filters.get("system"):
            conditions.append("system = ?")
            params.append(filters["system"])
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        sql = (
            "SELECT recording_id, title, episode_title, channel, genre, "
            "system, record_date, air_date FROM recordings"
            f"{where} ORDER BY record_date DESC LIMIT ?"
        )
        try:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError as e:
            logger.error("list_recordings error: %s", e)
            return []

    def search_transcripts(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        filters = filters or {}
        # Sanitize user query for FTS5: extract alphanumeric tokens and quote each
        # to avoid syntax errors from punctuation like '&', "'", '-', etc.
        import re as _re
        _tokens = [t for t in _re.findall(r"[A-Za-z0-9]+", query or "") if t]
        if not _tokens:
            return []
        # FTS5 ANDs quoted terms by default, so a full natural-language
        # question matches nothing. Try strict AND first, then fall back to OR
        # (bm25-ranked) so multi-word questions still surface relevant chunks.
        _and_query = " AND ".join(f'"{t}"' for t in _tokens)
        _or_query = " OR ".join(f'"{t}"' for t in _tokens)
        _fts_query = _and_query
        conditions = ["transcript_fts MATCH ?"]
        params: list = [_fts_query]

        if filters.get("actor"):
            conditions.append(
                "r.recording_id IN (SELECT recording_id FROM actors WHERE actor_name LIKE ?)"
            )
            params.append(f"%{filters['actor']}%")
        if filters.get("genre"):
            conditions.append("r.genre LIKE ?")
            params.append(f"%{filters['genre']}%")
        if filters.get("channel"):
            conditions.append("(r.channel = ? OR r.channel_number = ?)")
            params.extend([filters["channel"], filters["channel"]])
        _df = self._coerce_date_to_epoch(filters.get("date_from"))
        if _df is not None:
            conditions.append("r.record_date >= ?")
            params.append(_df)
        _dt2 = self._coerce_date_to_epoch(filters.get("date_to"), end_of_day=True)
        if _dt2 is not None:
            conditions.append("r.record_date <= ?")
            params.append(_dt2)
        if filters.get("system"):
            conditions.append("r.system = ?")
            params.append(filters["system"])

        where = " AND ".join(conditions)

        # Fetch a generous candidate pool (bm25-ranked chunks), then re-rank so
        # that the recording which is genuinely *about* the query — i.e. the one
        # with the most matching chunks — wins, instead of a single coincidental
        # chunk in an unrelated show. Without this, a 4-word query like
        # "cleveland browns chicago bears" could match one stray chunk in some
        # sitcom and never surface the actual game (which matches "browns"/
        # "bears" across ~200 chunks but rarely all four words in one 30s slice).
        _CANDIDATE_CAP = max(limit * 25, 400)
        sql = f"""
            SELECT r.recording_id, r.title, r.episode_title, r.channel,
                   r.genre, r.system, r.record_date, r.air_date,
                   tc.chunk_index, tc.start_time, tc.end_time,
                   snippet(transcript_fts, 0, '<b>', '</b>', '...', 32) AS snippet,
                   rank
            FROM transcript_fts
            JOIN transcript_chunks tc ON tc.chunk_id = transcript_fts.rowid
            JOIN recordings r ON r.recording_id = tc.recording_id
            WHERE {where}
            ORDER BY rank
            LIMIT ?
        """

        def _fetch(fts_query: str) -> list:
            p = list(params)
            p[0] = fts_query
            p.append(_CANDIDATE_CAP)
            return self._conn.execute(sql, p).fetchall()

        try:
            rows = _fetch(_and_query)
            # Fall back to OR when strict AND is too sparse to establish which
            # recording the query is about (a single AND hit is usually a fluke).
            if len(rows) < max(3, limit) and _or_query != _and_query:
                or_rows = _fetch(_or_query)
                if len(or_rows) > len(rows):
                    rows = or_rows
        except sqlite3.OperationalError as e:
            logger.error("FTS search error: %s", e)
            return []

        if not rows:
            return []

        # Re-rank: recordings with more matching chunks first (they are the real
        # topical match), preserving bm25 chunk rank as the tiebreaker within a
        # recording. `rank` is bm25 (more negative = better).
        from collections import Counter
        rec_hits = Counter(row["recording_id"] for row in rows)
        indexed = list(enumerate(rows))
        indexed.sort(key=lambda ir: (-rec_hits[ir[1]["recording_id"]], ir[1]["rank"], ir[0]))
        ordered = [dict(row) for _, row in indexed]
        return ordered[offset:offset + limit]

    def search_by_actor(self, actor_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT r.recording_id, r.title, r.episode_title, r.system,
                      r.channel, r.record_date, a.role, a.billing_order
               FROM actors a
               JOIN recordings r ON r.recording_id = a.recording_id
               WHERE a.actor_name LIKE ?
               ORDER BY r.record_date DESC
               LIMIT ?""",
            (f"%{actor_name}%", limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> Dict[str, Any]:
        rec = self._conn.execute("SELECT COUNT(*) as c FROM recordings").fetchone()
        chunks = self._conn.execute("SELECT COUNT(*) as c FROM transcript_chunks").fetchone()
        actors = self._conn.execute("SELECT COUNT(DISTINCT actor_name) as c FROM actors").fetchone()
        return {
            "total_recordings": rec["c"],
            "total_chunks": chunks["c"],
            "distinct_actors": actors["c"],
        }

    def rebuild_fts(self) -> None:
        self._conn.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild')")
        self._conn.commit()
        logger.info("FTS5 index rebuilt")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            logger.info("Transcript index closed")
