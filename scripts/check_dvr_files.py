#!/usr/bin/env python3
"""Check DVR files endpoint for yesterday's recordings."""
import json, urllib.request
from datetime import datetime, timedelta

yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
print(f"Looking for recordings from: {yesterday}\n")

resp = urllib.request.urlopen("http://localhost:8089/dvr/files", timeout=10)
files = json.loads(resp.read().decode())
print(f"Total DVR files: {len(files)}")

# Find recordings from yesterday
yesterday_files = []
for f in files:
    dr = f.get("DateRecorded", "")
    if isinstance(dr, str) and yesterday in dr:
        yesterday_files.append(f)

print(f"Files from {yesterday}: {len(yesterday_files)}\n")

if yesterday_files:
    for f in yesterday_files:
        title = f.get("Title", "?")
        ep_title = f.get("EpisodeTitle", "")
        season = f.get("SeasonNumber", "")
        episode = f.get("EpisodeNumber", "")
        se = f"S{season:02d}E{episode:02d}" if isinstance(season, int) and isinstance(episode, int) else ""
        path = f.get("Path", "")
        dr = f.get("DateRecorded", "")
        fid = f.get("ID", "?")
        print(f"  [{fid}] {title} {se} {ep_title}")
        print(f"    DateRecorded: {dr}")
        print(f"    Path: {path}")
        print()
else:
    # Show last 10 recordings
    print("Last 10 recordings:")
    dated = sorted(files, key=lambda x: x.get("DateRecorded", ""), reverse=True)
    for f in dated[:10]:
        title = f.get("Title", "?")
        ep_title = f.get("EpisodeTitle", "")
        dr = f.get("DateRecorded", "")
        print(f"  {dr}: {title} - {ep_title}")
