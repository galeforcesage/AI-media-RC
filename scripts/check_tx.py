#!/usr/bin/env python3
"""Quick check of transcription databases."""
import sqlite3, os, glob

# Check databases
for db in [
    "/home/USER_HOME/AI-media-RC/backend/transcription/transcription.db",
    "/home/USER_HOME/AI-media-RC/backend/transcription/transcript_index.db",
]:
    print(f"\n=== {os.path.basename(db)} ===")
    if not os.path.exists(db):
        print("  File not found")
        continue
    try:
        c = sqlite3.connect(db)
        cur = c.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        print(f"Tables: {tables}")
        for t in tables:
            cur.execute(f"SELECT COUNT(*) FROM [{t}]")
            cnt = cur.fetchone()[0]
            print(f"  {t}: {cnt} rows")
            if cnt > 0:
                cur.execute(f"SELECT * FROM [{t}] ORDER BY rowid DESC LIMIT 5")
                cols = [d[0] for d in cur.description]
                print(f"  Columns: {cols}")
                for row in cur.fetchall():
                    print(f"    {row}")
        c.close()
    except Exception as e:
        print(f"Error: {e}")

# Check for Channels DVR recording paths
print("\n=== Channels DVR recordings ===")
channels_paths = ["/home/USER_HOME/DVR", "/mnt/dvr", "/home/USER_HOME/channels-dvr", "/var/channels"]
for p in channels_paths:
    if os.path.exists(p):
        print(f"Found: {p}")
        # List recent files
        for f in sorted(glob.glob(os.path.join(p, "**/*.ts"), recursive=True))[-5:]:
            print(f"  {f} ({os.path.getsize(f)/(1024*1024):.0f}MB)")
    
# Check watcher config / sidecar files
print("\n=== JSON sidecars ===")
sidecar_dirs = ["/home/USER_HOME/AI-media-RC/backend/transcription"]
for d in sidecar_dirs:
    jsons = glob.glob(os.path.join(d, "**/*.json"), recursive=True)[:5]
    for j in jsons:
        print(f"  {j}")
