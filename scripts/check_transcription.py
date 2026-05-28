#!/usr/bin/env python3
"""Check transcription status for The Neighborhood April 6 2026."""
import requests, json, os, glob

# 1. Check for sidecar JSON files
print("=== Sidecar JSON files ===")
base = "/dvr/TV/The Neighborhood"
for root, dirs, files in os.walk(base):
    for f in files:
        if f.endswith(".json") and "2026-04-06" in f:
            fp = os.path.join(root, f)
            print(f"  FOUND: {fp} ({os.path.getsize(fp)} bytes)")

# Also try common DVR paths
for base2 in ["/mnt/dvr/TV/The Neighborhood", "/mnt/media/TV/The Neighborhood",
              "/home/USER_HOME/dvr/TV/The Neighborhood"]:
    if os.path.isdir(base2):
        for root, dirs, files in os.walk(base2):
            for f in files:
                if f.endswith(".json"):
                    fp = os.path.join(root, f)
                    print(f"  FOUND: {fp} ({os.path.getsize(fp)} bytes)")

# 2. Check transcription service index
print("\n=== Transcription search ===")
try:
    r = requests.get("http://localhost:8770/api/search", params={"q": "neighborhood"}, timeout=5)
    data = r.json()
    results = data.get("results", [])
    print(f"  {len(results)} results")
    for h in results[:5]:
        print(f"  - {h.get('title','')} {h.get('episode_title','')} {h.get('air_date','')}")
except Exception as e:
    print(f"  Error: {e}")

# 3. Check orchestrator search
print("\n=== Orchestrator search ===")
try:
    r = requests.get("http://localhost:8000/api/search", params={"q": "neighborhood april 6"}, timeout=10)
    data = r.json()
    print(json.dumps(data, indent=2)[:500])
except Exception as e:
    print(f"  Error: {e}")

# 4. Find the recording path and check for sidecar
print("\n=== Recording file check ===")
r = requests.get("http://localhost:8089/dvr/files")
for rec in r.json():
    if rec.get("ID") == "12260":
        path = rec.get("Path", "")
        print(f"  Recording path: {path}")
        # Check common base dirs
        for base in ["/dvr", "/mnt/dvr", "/mnt/media"]:
            full = os.path.join(base, path)
            mpg = full
            sidecar = os.path.splitext(full)[0] + ".json"
            print(f"  Check {mpg}: exists={os.path.exists(mpg)}")
            print(f"  Check {sidecar}: exists={os.path.exists(sidecar)}")
        break
