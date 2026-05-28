#!/usr/bin/env python3
import requests, json, os, glob

# Check DVR recordings
print("=== DVR Recordings matching 'Neighborhood' ===")
r = requests.get("http://localhost:8089/dvr/files")
recs = r.json()
for rec in recs:
    a = rec.get("Airing", {})
    title = a.get("Title", "")
    if "eighbor" in title.lower():
        ep = a.get("EpisodeTitle", "")
        air_time = a.get("Time", 0)
        from datetime import datetime
        dt = datetime.fromtimestamp(air_time) if air_time else None
        print(f"  ID={rec['ID']}  {title} - {ep}  aired={dt}  duration={rec.get('Duration',0):.0f}s")
        print(f"    Path: {rec.get('Path','?')}")

# Check transcription sidecar files
print("\n=== Transcription sidecar files matching 'Neighborhood' ===")
for pattern in ["/mnt/dvr/**/*eighbor*/*.json", "/mnt/dvr/**/*eighbor*.json",
                "/home/USER_HOME/**/*eighbor*.json", "/mnt/media/**/*eighbor*.json"]:
    for f in glob.glob(pattern, recursive=True):
        print(f"  {f}")

# Also check the transcription store/index
print("\n=== Transcription search via orchestrator ===")
try:
    r = requests.get("http://localhost:8000/api/search", params={"q": "neighborhood"})
    data = r.json()
    for key, val in data.items():
        if isinstance(val, dict) and val.get("results"):
            for hit in val["results"][:5]:
                print(f"  {key}: {hit.get('title','')} - {hit.get('episode_title','')} score={hit.get('score','')}")
except Exception as e:
    print(f"  Error: {e}")
