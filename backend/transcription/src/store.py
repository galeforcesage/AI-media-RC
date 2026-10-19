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
            if "transcript_state" not in cols:
                self._conn.execute(
                    "ALTER TABLE transcripts ADD COLUMN transcript_state TEXT DEFAULT 'final'"
                )
                logger.info("transcripts: added 'transcript_state' column")
        except Exception:
            logger.exception("Failed to add 'source' column (non-fatal)")

        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS transcript_segments (
                recording_id TEXT NOT NULL,
                segment_key TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                cc_text TEXT DEFAULT '',
                cc_timestamp REAL,
                asr_text TEXT DEFAULT '',
                asr_word_timestamps TEXT DEFAULT '[]',
                asr_confidence REAL,
                speaker_id TEXT,
                resolved_text TEXT DEFAULT '',
                resolved_source TEXT DEFAULT '',
                resolution_confidence REAL DEFAULT 0,
                commercial_probability REAL,
                comskip_state TEXT DEFAULT '',
                transcript_state TEXT NOT NULL DEFAULT 'live',
                updated_at REAL NOT NULL,
                PRIMARY KEY (recording_id, segment_key)
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_segments_recording_time "
            "ON transcript_segments(recording_id, start_time)"
        )

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
        except sqlite3.DatabaseError as e:
            logger.warning("FTS5 index corrupt (%s), rebuilding...", e)
            try:
                self._conn.execute(
                    "INSERT INTO transcripts_fts(transcripts_fts) VALUES('rebuild')"
                )
                self._conn.commit()
                logger.info("FTS5 index rebuilt successfully")
            except sqlite3.DatabaseError:
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
                transcript, summary, keywords, topics, scenes, vtt, source,
                transcript_state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (meta.recording_id, meta.system, meta.title, meta.episode,
             meta.duration, meta.word_count, meta.transcript, meta.summary,
             json.dumps(meta.keywords), json.dumps(meta.topics),
             json.dumps(meta.scenes), meta.vtt,
             getattr(meta, "source", "stt") or "stt",
             getattr(meta, "transcript_state", "final") or "final",
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
        self._conn.execute(
            "DELETE FROM transcript_segments WHERE recording_id = ?", (recording_id,)
        )
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
        except sqlite3.DatabaseError as exc:
            # SQLITE_CORRUPT ("database disk image is malformed") can surface on a
            # long-lived connection whose FTS shadow state went bad even when the
            # on-disk DB is healthy. Heal the FTS index once and retry so a single
            # bad query doesn't take transcript search down until a restart.
            logger.warning("FTS search hit DatabaseError for %r: %s — healing and retrying", query, exc)
            try:
                self._check_fts_integrity()
                rows = self._conn.execute(_sql, (and_query, limit)).fetchall()
                if not rows and or_query != and_query:
                    rows = self._conn.execute(_sql, (or_query, limit)).fetchall()
            except sqlite3.DatabaseError as exc2:
                logger.error("FTS search still failing after heal for %r: %s", query, exc2)
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
            "progressive_states": {
                row["transcript_state"]: row["c"]
                for row in self._conn.execute(
                    "SELECT COALESCE(transcript_state, 'final') AS transcript_state, "
                    "COUNT(*) AS c FROM transcripts GROUP BY transcript_state"
                ).fetchall()
            },
        }

    def ingest_evidence(
        self,
        recording_id: str,
        system: str,
        segments: List[Dict],
        evidence: str,
        final: bool = False,
    ) -> None:
        """Idempotently upsert CC or ASR evidence and rematerialize the transcript."""
        if evidence not in {"cc", "asr"}:
            raise ValueError(f"Unsupported transcript evidence: {evidence}")
        now = time.time()
        for segment in segments:
            start = round(float(segment.get("start", 0)), 2)
            end = round(float(segment.get("end", start)), 2)
            text = str(segment.get("text", "")).strip()
            if not text or end <= start:
                continue
            speaker = segment.get("speaker")
            if evidence == "cc":
                from .cc_extractor import strip_speaker_prefix
                text, hint = strip_speaker_prefix(text)
                speaker = speaker or hint
            key = f"{evidence}:{int(round(start * 1000))}"
            if evidence == "asr":
                candidates = self._conn.execute(
                    """SELECT segment_key, start_time, end_time, asr_text
                       FROM transcript_segments
                       WHERE recording_id=? AND segment_key LIKE 'asr:%'
                         AND start_time < ? AND end_time > ?""",
                    (recording_id, end, start),
                ).fetchall()
                for candidate in candidates:
                    overlap = min(end, candidate["end_time"]) - max(
                        start, candidate["start_time"]
                    )
                    shorter = max(
                        0.001,
                        min(end - start, candidate["end_time"] - candidate["start_time"]),
                    )
                    similarity = __import__("difflib").SequenceMatcher(
                        None, text.lower(), candidate["asr_text"].lower()
                    ).ratio()
                    if overlap / shorter >= 0.5 and similarity >= 0.6:
                        key = candidate["segment_key"]
                        break
            state = "final" if final else "live"
            if evidence == "cc":
                self._conn.execute(
                    """INSERT INTO transcript_segments
                       (recording_id, segment_key, start_time, end_time, cc_text,
                        cc_timestamp, speaker_id, transcript_state, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(recording_id, segment_key) DO UPDATE SET
                         start_time=excluded.start_time, end_time=excluded.end_time,
                         cc_text=excluded.cc_text, cc_timestamp=excluded.cc_timestamp,
                         speaker_id=COALESCE(excluded.speaker_id, speaker_id),
                         transcript_state=CASE
                           WHEN transcript_state='final' THEN 'final'
                           ELSE excluded.transcript_state END,
                         updated_at=excluded.updated_at""",
                    (recording_id, key, start, end, text, now, speaker, state, now),
                )
            else:
                self._conn.execute(
                    """INSERT INTO transcript_segments
                       (recording_id, segment_key, start_time, end_time, asr_text,
                        asr_word_timestamps, asr_confidence, speaker_id,
                        transcript_state, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(recording_id, segment_key) DO UPDATE SET
                         start_time=excluded.start_time, end_time=excluded.end_time,
                         asr_text=excluded.asr_text,
                         asr_word_timestamps=excluded.asr_word_timestamps,
                         asr_confidence=excluded.asr_confidence,
                         speaker_id=COALESCE(excluded.speaker_id, speaker_id),
                         transcript_state=CASE
                           WHEN transcript_state='final' THEN 'final'
                           ELSE excluded.transcript_state END,
                         updated_at=excluded.updated_at""",
                    (
                        recording_id, key, start, end, text,
                        json.dumps(segment.get("words") or []),
                        segment.get("confidence"), speaker, state, now,
                    ),
                )
        self._reconcile_segments(recording_id, final=final)
        self._materialize_progressive(recording_id, system)
        self._conn.commit()

    def _reconcile_segments(self, recording_id: str, final: bool = False) -> None:
        from .progressive import resolve_text

        rows = self._conn.execute(
            "SELECT * FROM transcript_segments WHERE recording_id = ? "
            "ORDER BY start_time, end_time",
            (recording_id,),
        ).fetchall()
        cc_rows = [r for r in rows if r["segment_key"].startswith("cc:")]
        asr_rows = [r for r in rows if r["segment_key"].startswith("asr:")]
        matched_asr = set()

        for cc in cc_rows:
            best = None
            best_overlap = 0.0
            for asr in asr_rows:
                overlap = min(cc["end_time"], asr["end_time"]) - max(
                    cc["start_time"], asr["start_time"]
                )
                if overlap > best_overlap:
                    best, best_overlap = asr, overlap
            asr_text = best["asr_text"] if best is not None and best_overlap > 0 else ""
            if best is not None and best_overlap > 0:
                matched_asr.add(best["segment_key"])
            text, source, confidence = resolve_text(cc["cc_text"], asr_text)
            state = "final" if final or cc["transcript_state"] == "final" else (
                "stable" if asr_text else "live"
            )
            self._conn.execute(
                """UPDATE transcript_segments
                   SET asr_text=CASE WHEN ? != '' THEN ? ELSE asr_text END,
                       resolved_text=?, resolved_source=?,
                       resolution_confidence=?, transcript_state=?, updated_at=?
                   WHERE recording_id=? AND segment_key=?""",
                (
                    asr_text, asr_text, text, source, confidence, state, time.time(),
                    recording_id, cc["segment_key"],
                ),
            )

        for asr in asr_rows:
            if asr["segment_key"] in matched_asr:
                self._conn.execute(
                    "UPDATE transcript_segments SET resolved_text='' "
                    "WHERE recording_id=? AND segment_key=?",
                    (recording_id, asr["segment_key"]),
                )
                continue
            text, source, confidence = resolve_text("", asr["asr_text"])
            state = "final" if final or asr["transcript_state"] == "final" else "live"
            self._conn.execute(
                """UPDATE transcript_segments
                   SET resolved_text=?, resolved_source=?,
                       resolution_confidence=?, transcript_state=?, updated_at=?
                   WHERE recording_id=? AND segment_key=?""",
                (
                    text, source, confidence, state, time.time(),
                    recording_id, asr["segment_key"],
                ),
            )

    def finalize_recording(self, recording_id: str, system: str) -> None:
        self._conn.execute(
            "UPDATE transcript_segments SET transcript_state='final', updated_at=? "
            "WHERE recording_id=?",
            (time.time(), recording_id),
        )
        self._reconcile_segments(recording_id, final=True)
        self._materialize_progressive(recording_id, system)
        self._conn.commit()

    def get_segments(self, recording_id: str) -> List[Dict]:
        rows = self._conn.execute(
            """SELECT segment_key, start_time AS start, end_time AS end,
                      cc_text, asr_text, asr_word_timestamps, asr_confidence,
                      speaker_id AS speaker, resolved_text AS text,
                      resolved_source AS source,
                      resolution_confidence AS confidence,
                      commercial_probability, comskip_state,
                      transcript_state AS state
               FROM transcript_segments
               WHERE recording_id=? AND resolved_text != ''
               ORDER BY start_time, end_time""",
            (recording_id,),
        ).fetchall()
        result = []
        for row in rows:
            segment = dict(row)
            try:
                segment["asr_word_timestamps"] = json.loads(
                    segment["asr_word_timestamps"] or "[]"
                )
            except (TypeError, json.JSONDecodeError):
                segment["asr_word_timestamps"] = []
            result.append(segment)
        return result

    def get_evidence_end(self, recording_id: str, evidence: str) -> float:
        column = "cc_text" if evidence == "cc" else "asr_text"
        row = self._conn.execute(
            f"SELECT COALESCE(MAX(end_time), 0) FROM transcript_segments "
            f"WHERE recording_id=? AND {column} != ''",
            (recording_id,),
        ).fetchone()
        return float(row[0] or 0)

    def get_transcript_state(self, recording_id: str) -> str:
        row = self._conn.execute(
            """SELECT CASE
                 WHEN SUM(transcript_state='live') > 0 THEN 'live'
                 WHEN SUM(transcript_state='stable') > 0 THEN 'stable'
                 ELSE 'final' END
               FROM transcript_segments
               WHERE recording_id=? AND resolved_text != ''""",
            (recording_id,),
        ).fetchone()
        return str(row[0] or "live")

    def _materialize_progressive(self, recording_id: str, system: str) -> None:
        segments = self.get_segments(recording_id)
        if not segments:
            return
        transcript = " ".join(segment["text"] for segment in segments).strip()
        duration = max(float(segment["end"]) for segment in segments)
        state = self.get_transcript_state(recording_id)
        vtt_lines = ["WEBVTT", ""]
        for segment in segments:
            vtt_lines.extend([
                f"{self._vtt_time(segment['start'])} --> {self._vtt_time(segment['end'])}",
                (
                    f"<v {segment['speaker']}>{segment['text']}"
                    if segment.get("speaker") else segment["text"]
                ),
                "",
            ])
        existing = self._conn.execute(
            "SELECT * FROM transcripts WHERE recording_id=?", (recording_id,)
        ).fetchone()
        title = existing["title"] if existing else recording_id
        episode = existing["episode"] if existing else ""
        summary = existing["summary"] if existing else ""
        keywords = existing["keywords"] if existing else "[]"
        topics = existing["topics"] if existing else "[]"
        scenes = existing["scenes"] if existing else "[]"
        self._conn.execute(
            """INSERT INTO transcripts
               (recording_id, system, title, episode, duration, word_count,
                transcript, summary, keywords, topics, scenes, vtt, source,
                transcript_state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'progressive', ?, ?)
               ON CONFLICT(recording_id) DO UPDATE SET
                 system=excluded.system, duration=excluded.duration,
                 word_count=excluded.word_count, transcript=excluded.transcript,
                 vtt=excluded.vtt, source='progressive',
                 transcript_state=excluded.transcript_state,
                 created_at=excluded.created_at""",
            (
                recording_id, system, title, episode, duration,
                len(transcript.split()), transcript, summary, keywords, topics,
                scenes, "\n".join(vtt_lines), state, time.time(),
            ),
        )

    @staticmethod
    def _vtt_time(seconds: float) -> str:
        total_ms = int(round(float(seconds) * 1000))
        hours, rem = divmod(total_ms, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        secs, millis = divmod(rem, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

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
            transcript_state=(
                row["transcript_state"]
                if "transcript_state" in row.keys() and row["transcript_state"]
                else "final"
            ),
            segments=self.get_segments(row["recording_id"]),
            created_at=row["created_at"],
        )
