#!/usr/bin/env python3
"""Reset error jobs to pending for retry."""
import sqlite3, os
db = sqlite3.connect(os.path.expanduser("~/AI-media-RC/backend/transcription/transcription.db"))
db.execute("UPDATE jobs SET status='pending', error=NULL WHERE status='error'")
db.commit()
print(f"Reset {db.total_changes} error jobs to pending")
db.close()
