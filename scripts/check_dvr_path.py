#!/usr/bin/env python3
"""Find DVR recordings base path and check transcription coverage."""
import json, os, subprocess, glob

# 1. Get full details for recording 12260
import urllib.request
url = "http://localhost:8089/dvr/files/12260"
with urllib.request.urlopen(url) as r:
    rec = json.loads(r.read())

print("=== Recording 12260 full fields ===")
for k, v in rec.items():
    if k != "Airing":
        print(f"  {k}: {v}")
print(f"  Airing.Title: {rec.get('Airing',{}).get('Title')}")
print(f"  Airing.EpisodeTitle: {rec.get('Airing',{}).get('EpisodeTitle')}")

# 2. Try to find the actual file on disk
path = rec.get("Path", "")
print(f"\n=== Searching for file on disk ===")
print(f"  DVR Path field: {path}")

# Try common base paths
bases = ["/shares/DVR", "/mnt/dvr", "/dvr", "/home/USER_HOME/dvr", 
         "/shares", "/mnt/media", "/mnt", "/home/USER_HOME"]
for base in bases:
    full = os.path.join(base, path)
    if os.path.exists(full):
        print(f"  FOUND: {full}")
        break
    # Also check if base itself exists
    if os.path.isdir(base):
        print(f"  Base {base} exists, checking...")
        # Check for TV subfolder
        tv_path = os.path.join(base, "TV")
        if os.path.isdir(tv_path):
            print(f"    TV/ folder exists in {base}")

# 3. Use 'find' to locate a Neighborhood file
print("\n=== Searching filesystem for Neighborhood mpg files ===")
result = subprocess.run(
    ["find", "/", "-maxdepth", "6", "-name", "*Neighborhood*S08E15*", "-type", "f"],
    capture_output=True, text=True, timeout=30
)
if result.stdout.strip():
    for line in result.stdout.strip().split("\n"):
        print(f"  FOUND: {line}")
else:
    print("  Not found via find command")

# 4. Check Channels DVR storage settings
print("\n=== DVR settings ===")
try:
    with urllib.request.urlopen("http://localhost:8089/dvr") as r:
        dvr = json.loads(r.read())
    for k, v in dvr.items():
        if "path" in k.lower() or "dir" in k.lower() or "folder" in k.lower() or "storage" in k.lower():
            print(f"  {k}: {v}")
except Exception as e:
    print(f"  Error: {e}")

# 5. Check where transcription service looks
print("\n=== Transcription sidecar search ===")
result2 = subprocess.run(
    ["find", "/", "-maxdepth", "6", "-name", "*.transcript.json", "-type", "f"],
    capture_output=True, text=True, timeout=30
)
if result2.stdout.strip():
    for line in result2.stdout.strip().split("\n")[:10]:
        print(f"  {line}")
else:
    print("  No .transcript.json files found")

# Also check for .json sidecars near DVR files
result3 = subprocess.run(
    ["find", "/", "-maxdepth", "6", "-path", "*/TV/*", "-name", "*.json", "-type", "f"],
    capture_output=True, text=True, timeout=30
)
if result3.stdout.strip():
    for line in result3.stdout.strip().split("\n")[:10]:
        print(f"  TV JSON: {line}")
else:
    print("  No JSON sidecars in TV/ folders")
