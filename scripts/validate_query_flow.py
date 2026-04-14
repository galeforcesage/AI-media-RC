#!/usr/bin/env python3
"""Validate the query flow for 'what was recorded yesterday'."""
import json, urllib.request, sys, os
from datetime import datetime, timedelta

yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
today = datetime.now().strftime("%Y-%m-%d")
print(f"=== Today: {today}, Yesterday: {yesterday} ===\n")

# 1. Check Channels DVR recordings directly
print("--- Channels DVR API: recordings from yesterday ---")
try:
    resp = urllib.request.urlopen("http://localhost:8089/dvr/recordings", timeout=10)
    recs = json.loads(resp.read().decode())
    yesterday_recs = []
    for r in recs:
        # Check multiple date fields
        date_rec = r.get("DateRecorded", "")
        date_aired = r.get("OriginalDate", "")
        date_added = r.get("DateAdded", "")
        # DateRecorded is usually epoch or ISO
        rec_date = ""
        if isinstance(date_rec, str) and yesterday in date_rec:
            rec_date = date_rec
        elif isinstance(date_rec, (int, float)) and date_rec > 0:
            from datetime import datetime as dt
            rec_dt = dt.fromtimestamp(date_rec)
            if rec_dt.strftime("%Y-%m-%d") == yesterday:
                rec_date = rec_dt.isoformat()
        
        if rec_date or yesterday in str(date_aired) or yesterday in str(date_added):
            yesterday_recs.append(r)
            title = r.get("Title", "?")
            ep = r.get("EpisodeTitle", "")
            season = r.get("SeasonNumber", "")
            episode = r.get("EpisodeNumber", "")
            se = f"S{season:02d}E{episode:02d}" if season and episode else ""
            path = r.get("Path", "")[-80:] if r.get("Path") else ""
            print(f"  ID={r.get('ID','?')}: {title} {se} {ep}")
            print(f"    DateRecorded={date_rec} OriginalDate={date_aired}")
            print(f"    Path=...{path}")
    
    if not yesterday_recs:
        print("  (none found by date match)")
        print(f"\n  Total recordings in DVR: {len(recs)}")
        # Show last 5 recordings sorted by date
        print("\n  Last 5 recordings (by DateRecorded):")
        dated = [(r, r.get("DateRecorded", 0)) for r in recs if isinstance(r.get("DateRecorded"), (int, float))]
        dated.sort(key=lambda x: x[1], reverse=True)
        for r, dr in dated[:5]:
            from datetime import datetime as dt
            title = r.get("Title", "?")
            ep = r.get("EpisodeTitle", "")
            print(f"    {dt.fromtimestamp(dr).isoformat()}: {title} - {ep}")
except Exception as e:
    print(f"  ERROR: {e}")

# 2. Check MCP channels_search_recordings
print("\n--- MCP channels_search_recordings ---")
try:
    # Call via orchestrator's internal MCP
    data = json.dumps({"prompt": f"Use the channels_search_recordings tool with start_date={yesterday} end_date={today} and show me the raw results", "systems": ["channelsdvr"]}).encode()
    # Actually let's just call the Channels DVR API with a date filter directly
    resp = urllib.request.urlopen(f"http://localhost:8089/dvr/recordings?date={yesterday}", timeout=10)
    result = json.loads(resp.read().decode())
    print(f"  DVR API returned {len(result)} recordings for date={yesterday}")
    for r in result[:5]:
        print(f"    {r.get('Title', '?')} - {r.get('EpisodeTitle', '')}")
except Exception as e:
    print(f"  ERROR: {e}")

# 3. Check transcripts for yesterday's recordings
print("\n--- Transcript sidecars ---")
sidecar_dir = os.path.expanduser("~/AI-media-RC/backend/transcription/sidecars")
if os.path.isdir(sidecar_dir):
    sidecars = os.listdir(sidecar_dir)
    print(f"  Total sidecars: {len(sidecars)}")
    yesterday_sidecars = [s for s in sidecars if yesterday in s]
    print(f"  Sidecars with '{yesterday}': {len(yesterday_sidecars)}")
    for s in yesterday_sidecars[:10]:
        print(f"    {s}")
else:
    print(f"  Sidecar dir not found: {sidecar_dir}")

# 4. Check what the semantic index knows
print("\n--- Semantic context for 'recorded yesterday' ---")
# We can't easily query the semantic index externally, but we can check the orchestrator log
print("  (check orchestrator log for semantic hits)")

print("\nDone.")
