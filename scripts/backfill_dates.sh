#!/bin/bash
# Backfill record_date from filename patterns in transcript_index.db
python3 << 'EOF'
import sqlite3, os, re
from datetime import datetime

db = "/home/USER_HOME/AI-media-RC/backend/transcription/transcript_index.db"
conn = sqlite3.connect(db)

# Get all recordings with NULL record_date
rows = conn.execute("""
    SELECT rowid, recording_id, file_path, title
    FROM recordings
    WHERE record_date IS NULL
""").fetchall()

print(f"Found {len(rows)} recordings with NULL record_date")

pattern = re.compile(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})')
updated = 0
skipped = 0

for rowid, rec_id, file_path, title in rows:
    # Try file_path first, then recording_id, then title
    source = file_path or rec_id or title or ""
    # Strip extension for matching
    stem = os.path.splitext(os.path.basename(source))[0] if source else ""
    m = pattern.search(stem)
    if not m:
        # Also try the recording_id directly
        m = pattern.search(rec_id or "")
    if not m:
        m = pattern.search(title or "")
    
    if m:
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5))
            )
            epoch = int(dt.timestamp())
            conn.execute(
                "UPDATE recordings SET record_date = ?, air_date = COALESCE(air_date, ?) WHERE rowid = ?",
                (epoch, epoch, rowid)
            )
            updated += 1
        except (ValueError, OSError):
            skipped += 1
    else:
        skipped += 1

conn.commit()
conn.close()

print(f"Updated: {updated}")
print(f"Skipped (no date pattern): {skipped}")
EOF
