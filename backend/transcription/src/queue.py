"""
queue.py
SQLite-backed persistent transcription queue.

Supports job lifecycle: pending -> extracting -> processing -> done/error.
Retry support with configurable max_attempts.
"""

from __future__ import annotations
import json
import logging
import sqlite3
import time
from typing import Dict, List, Optional

from .models import TranscriptionJob

logger = logging.getLogger(__name__)


class TranscriptionQueue:
    """Persistent job queue backed by SQLite."""

    def __init__(self, db_path: str = "transcription.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                system TEXT NOT NULL,
                recording_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                temp_audio_path TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                error TEXT DEFAULT '',
                created_at REAL,
                updated_at REAL,
                duration REAL DEFAULT 0
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS job_checkpoints (
                job_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'stt',
                completed_seconds REAL NOT NULL DEFAULT 0,
                audio_duration REAL NOT NULL DEFAULT 0,
                segments_json TEXT NOT NULL DEFAULT '[]',
                updated_at REAL NOT NULL
            )
        """)
        checkpoint_columns = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(job_checkpoints)"
            ).fetchall()
        }
        if "mode" not in checkpoint_columns:
            self._conn.execute(
                "ALTER TABLE job_checkpoints ADD COLUMN mode TEXT NOT NULL DEFAULT 'stt'"
            )
        self._conn.commit()
        logger.info("Transcription queue opened: %s", self.db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def enqueue(self, job: TranscriptionJob) -> TranscriptionJob:
        # Skip if duplicate recording_id already pending/processing
        existing = self._conn.execute(
            "SELECT job_id FROM jobs WHERE recording_id = ? AND status IN ('pending', 'extracting', 'processing')",
            (job.recording_id,),
        ).fetchone()
        if existing:
            logger.debug("Job for recording %s already queued", job.recording_id)
            return job
        # For incremental jobs, also dedup on file_path — don't allow multiple
        # pending/processing jobs for the same physical file
        if "__inc_" in job.recording_id:
            existing_path = self._conn.execute(
                "SELECT job_id FROM jobs WHERE file_path = ? AND status IN ('pending', 'extracting', 'processing')",
                (job.file_path,),
            ).fetchone()
            if existing_path:
                logger.debug("Incremental job for file %s already queued (job %s)",
                             job.file_path, existing_path["job_id"])
                return job
        self._conn.execute(
            """INSERT INTO jobs (job_id, system, recording_id, file_path, temp_audio_path,
                status, attempts, max_attempts, error, created_at, updated_at, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job.job_id, job.system, job.recording_id, job.file_path,
             job.temp_audio_path, job.status, job.attempts, job.max_attempts,
             job.error, job.created_at, job.updated_at, job.duration),
        )
        self._conn.commit()
        logger.info("Enqueued job %s for %s (%s)", job.job_id, job.recording_id, job.system)
        return job

    def dequeue(self) -> Optional[TranscriptionJob]:
        """Get the next pending job (oldest first)."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return self._row_to_job(row)

    def update_status(self, job_id: str, status: str, error: str = "", **extra) -> None:
        sets = ["status = ?", "updated_at = ?"]
        vals = [status, time.time()]
        if error:
            sets.append("error = ?")
            vals.append(error)
        if status in ("extracting", "processing"):
            sets.append("attempts = attempts + 1")
        for k, v in extra.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(job_id)
        self._conn.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", vals
        )
        self._conn.commit()

    def mark_for_retry(self, job_id: str, error: str) -> bool:
        """Mark a failed job for retry, or set to error if max attempts reached."""
        row = self._conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            return False
        if row["attempts"] >= row["max_attempts"]:
            self.update_status(job_id, "error", error=error)
            return False
        self.update_status(job_id, "pending", error=error)
        return True

    def save_checkpoint(
        self,
        job_id: str,
        completed_seconds: float,
        audio_duration: float,
        segments: List[Dict],
        mode: str = "stt",
    ) -> None:
        """Durably record completed Whisper clips for duplicate-free resume."""
        self._conn.execute(
            """INSERT INTO job_checkpoints
               (job_id, mode, completed_seconds, audio_duration, segments_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                 mode=excluded.mode,
                 completed_seconds=excluded.completed_seconds,
                 audio_duration=excluded.audio_duration,
                 segments_json=excluded.segments_json,
                 updated_at=excluded.updated_at""",
            (
                job_id,
                mode,
                completed_seconds,
                audio_duration,
                json.dumps(segments),
                time.time(),
            ),
        )
        self._conn.commit()

    def load_checkpoint(self, job_id: str) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT * FROM job_checkpoints WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        try:
            segments = json.loads(row["segments_json"])
        except (TypeError, json.JSONDecodeError):
            logger.error("Discarding corrupt transcription checkpoint for %s", job_id)
            self.clear_checkpoint(job_id)
            return None
        return {
            "mode": row["mode"],
            "completed_seconds": float(row["completed_seconds"]),
            "audio_duration": float(row["audio_duration"]),
            "segments": segments,
        }

    def clear_checkpoint(self, job_id: str) -> None:
        self._conn.execute("DELETE FROM job_checkpoints WHERE job_id = ?", (job_id,))
        self._conn.commit()

    def requeue_incomplete(self, job_id: str, reason: str) -> None:
        """Return interrupted work to pending without consuming another attempt."""
        self._conn.execute(
            "UPDATE jobs SET status = 'pending', error = ?, updated_at = ? WHERE job_id = ?",
            (reason, time.time(), job_id),
        )
        self._conn.commit()

    def get_job(self, job_id: str) -> Optional[TranscriptionJob]:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return self._row_to_job(row)

    def list_jobs(self, status: Optional[str] = None, limit: int = 50) -> List[TranscriptionJob]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def stats(self) -> Dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def find_stale_jobs(self, stale_seconds: int = 1800) -> List[Dict]:
        """Find jobs in 'extracting' or 'processing' state that have not been
        updated in `stale_seconds` (default 30 min). These are likely hung."""
        cutoff = time.time() - stale_seconds
        rows = self._conn.execute(
            """SELECT job_id, system, recording_id, status, attempts,
                      created_at, updated_at, error
               FROM jobs
               WHERE status IN ('extracting', 'processing')
                 AND updated_at < ?
               ORDER BY updated_at ASC""",
            (cutoff,),
        ).fetchall()
        now = time.time()
        return [
            {
                "job_id": r["job_id"],
                "system": r["system"],
                "recording_id": r["recording_id"],
                "status": r["status"],
                "attempts": r["attempts"],
                "stuck_seconds": int(now - (r["updated_at"] or now)),
                "error": r["error"],
            }
            for r in rows
        ]

    def _row_to_job(self, row) -> TranscriptionJob:
        return TranscriptionJob(
            job_id=row["job_id"],
            system=row["system"],
            recording_id=row["recording_id"],
            file_path=row["file_path"],
            temp_audio_path=row["temp_audio_path"],
            status=row["status"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            duration=row["duration"],
        )
