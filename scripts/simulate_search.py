#!/usr/bin/env python3
"""Simulate channels_search_recordings for yesterday and measure result size."""
import json, urllib.request
from datetime import datetime, timedelta

yesterday = (datetime.now() - timedelta(days=1))
today = datetime.now()
start_epoch = int(yesterday.replace(hour=0, minute=0, second=0).timestamp())
end_epoch = int(today.replace(hour=23, minute=59, second=59).timestamp())

print(f"Searching {yesterday.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}")
print(f"Epoch range: {start_epoch} to {end_epoch}\n")

resp = urllib.request.urlopen("http://localhost:8089/dvr/files", timeout=10)
files = json.loads(resp.read().decode())

results = []
for rec in files:
    airing = rec.get("Airing") or {}
    rec_time = airing.get("Time") or rec.get("CreatedAt") or 0
    if start_epoch <= int(rec_time) <= end_epoch:
        results.append(rec)

print(f"Matching recordings: {len(results)}\n")

# Show what the tool would return (full enriched data)
for r in results:
    airing = r.get("Airing", {})
    title = airing.get("Title", "?")
    ep = airing.get("EpisodeTitle", "")
    season = airing.get("SeasonNumber", "")
    episode = airing.get("EpisodeNumber", "")
    se = f"S{season:02d}E{episode:02d}" if isinstance(season, int) and isinstance(episode, int) else ""
    air_time = airing.get("Time", 0)
    dt = datetime.fromtimestamp(air_time).strftime("%H:%M") if air_time else "?"
    print(f"  {title} {se} {ep} (recorded {dt})")
    print(f"    Path: {r.get('Path', '?')}")

# Check truncation
full_result = json.dumps({"success": True, "data": results}, default=str)
print(f"\nFull result JSON size: {len(full_result)} chars")
print(f"Agent truncates at 2000 chars — {'WILL BE TRUNCATED' if len(full_result) > 2000 else 'fits'}")

if len(full_result) > 2000:
    print(f"\nFirst 2000 chars of result:")
    print(full_result[:2000])
    print("\n... TRUNCATED ...")
    
    # Count how many recordings fit in 2000 chars
    for i in range(1, len(results)+1):
        partial = json.dumps({"success": True, "data": results[:i]}, default=str)
        if len(partial) > 2000:
            print(f"\nOnly {i-1} of {len(results)} recordings fit in 2000 chars")
            break
