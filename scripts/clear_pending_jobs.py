#!/usr/bin/env python3
"""Clear pending/extracting jobs from the transcription queue."""
import sqlite3
db = "/home/USER_HOME/AI-media-RC/backend/transcription/transcription.db"
c = sqlite3.connect(db)
cur = c.execute("DELETE FROM jobs WHERE status IN ('pending', 'extracting')")
c.commit()
print(f"Cleared {cur.rowcount} pending/extracting jobs")
c.close()
