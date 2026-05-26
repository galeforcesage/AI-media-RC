#!/usr/bin/env python3
"""Check transcription service status."""
import sqlite3, os, subprocess

# Check DB
db_paths = [
    "/home/{username}/AI-media-RC/backend/transcription/transcription.db",
    "/home/{username}/AI-media-RC/transcription.db",
    "/tmp/transcription.db",
]
for p in db_paths:
    if os.path.exists(p):
        print(f"DB found: {p} ({os.path.getsize(p)} bytes)")
        c = sqlite3.connect(p)
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print(f"  Tables: {tables}")
        for t in tables:
            cnt = c.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            print(f"  {t}: {cnt} rows")
            if cnt > 0 and cnt <= 10:
                cols = [d[0] for d in c.execute(f"SELECT * FROM [{t}] LIMIT 1").description]
                print(f"    Columns: {cols}")
                for row in c.execute(f"SELECT * FROM [{t}]").fetchall():
                    print(f"    {dict(zip(cols, row))}")
            elif cnt > 10:
                cols = [d[0] for d in c.execute(f"SELECT * FROM [{t}] LIMIT 1").description]
                print(f"    Columns: {cols}")
                # Show status breakdown for jobs
                if 'status' in cols:
                    for row in c.execute(f"SELECT status, COUNT(*) FROM [{t}] GROUP BY status").fetchall():
                        print(f"    Status {row[0]}: {row[1]}")
                # Show last 3
                for row in c.execute(f"SELECT * FROM [{t}] ORDER BY rowid DESC LIMIT 3").fetchall():
                    print(f"    Latest: {dict(zip(cols, row))}")
        c.close()
        break
else:
    print("No DB found!")
    # Find it
    result = subprocess.run(["find", "/home/{username}", "-name", "*.db", "-type", "f"], 
                          capture_output=True, text=True, timeout=10)
    print("DBs found:")
    for line in result.stdout.strip().split("\n"):
        if line:
            print(f"  {line}")

# Check service logs
print("\n=== Recent transcription logs ===")
log_paths = ["/tmp/transcription.log", "/tmp/mcp-transcription.log"]
for lp in log_paths:
    if os.path.exists(lp):
        print(f"\nLog: {lp} ({os.path.getsize(lp)} bytes)")
        with open(lp) as f:
            lines = f.readlines()
            # Show last 30 lines
            for line in lines[-30:]:
                print(f"  {line.rstrip()}")
        break
else:
    print("No log files found at expected paths")

# Check if whisper/ffmpeg available
print("\n=== Dependencies ===")
for cmd in ["ffmpeg", "whisper"]:
    result = subprocess.run(["which", cmd], capture_output=True, text=True)
    print(f"  {cmd}: {result.stdout.strip() or 'NOT FOUND'}")

# Check if faster-whisper is installed
result = subprocess.run(["pip3", "list"], capture_output=True, text=True)
for line in result.stdout.split("\n"):
    if "whisper" in line.lower() or "ctranslate" in line.lower():
        print(f"  {line.strip()}")
