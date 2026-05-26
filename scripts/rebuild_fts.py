"""Rebuild the FTS5 index in transcription.db."""
import sqlite3
import time
import sys

db_path = "/home/{username}/AI-media-RC/backend/transcription/transcription.db"

# First stop the service so we don't fight over the WAL
import subprocess
subprocess.run(["bash", "/home/{username}/AI-media-RC/scripts/watchdog.sh", "stop", "transcription"],
               capture_output=True, timeout=10)
time.sleep(2)

print(f"Opening {db_path}...")
conn = sqlite3.connect(db_path, timeout=30)
conn.execute("PRAGMA journal_mode=WAL")

# Check current state
try:
    r = conn.execute("SELECT count(*) FROM transcripts").fetchone()
    print(f"Base table: {r[0]} rows")
except Exception as e:
    print(f"Base table error: {e}")
    sys.exit(1)

# Test FTS before rebuild
try:
    r = conn.execute("SELECT count(*) FROM transcripts_fts WHERE transcripts_fts MATCH 'ncis'").fetchone()
    print(f"FTS before rebuild: {r[0]} matches for 'ncis'")
except Exception as e:
    print(f"FTS corrupt: {e}")

# Rebuild FTS5 from content table
print("Rebuilding FTS5 index...")
t0 = time.time()
try:
    conn.execute("INSERT INTO transcripts_fts(transcripts_fts) VALUES('rebuild')")
    conn.commit()
    elapsed = time.time() - t0
    print(f"FTS rebuild complete in {elapsed:.1f}s")
except Exception as e:
    print(f"FTS rebuild failed: {e}")
    print("Dropping and recreating FTS5...")
    conn.executescript("""
        DROP TABLE IF EXISTS transcripts_fts;
        CREATE VIRTUAL TABLE transcripts_fts USING fts5(
            recording_id, title, episode, transcript, summary, keywords,
            content='transcripts', content_rowid='rowid'
        );
        INSERT INTO transcripts_fts(transcripts_fts) VALUES('rebuild');
    """)
    conn.commit()
    print("FTS recreated and rebuilt from content table")

# Verify
try:
    r = conn.execute("SELECT count(*) FROM transcripts_fts WHERE transcripts_fts MATCH 'ncis'").fetchone()
    print(f"FTS after rebuild: {r[0]} matches for 'ncis'")
except Exception as e:
    print(f"FTS still broken: {e}")
    sys.exit(1)

conn.close()

# Restart service
print("Restarting transcription service...")
subprocess.run(["bash", "/home/{username}/AI-media-RC/scripts/watchdog.sh", "restart", "transcription"],
               capture_output=True, timeout=10)
print("Done!")
