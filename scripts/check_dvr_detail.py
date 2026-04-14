#!/usr/bin/env python3
"""Inspect DVR file data structure and find yesterday's recordings."""
import json, urllib.request
from datetime import datetime, timedelta

yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
yesterday_ts_start = int(datetime.strptime(yesterday, "%Y-%m-%d").timestamp())
yesterday_ts_end = yesterday_ts_start + 86400
print(f"Yesterday: {yesterday} (epoch {yesterday_ts_start} - {yesterday_ts_end})\n")

resp = urllib.request.urlopen("http://localhost:8089/dvr/files", timeout=10)
files = json.loads(resp.read().decode())
print(f"Total DVR files: {len(files)}")

# Show keys of first file
if files:
    print(f"\nSample file keys: {list(files[0].keys())[:20]}")
    # Show a sample with all fields
    sample = files[0]
    for k in sorted(sample.keys()):
        v = sample[k]
        if isinstance(v, str) and len(v) > 100:
            v = v[:100] + "..."
        print(f"  {k}: {v}")
    
    print(f"\n--- Looking for epoch timestamps between {yesterday_ts_start} and {yesterday_ts_end} ---")
    
    # Try to find date-like fields
    date_fields = []
    for k, v in sample.items():
        if isinstance(v, (int, float)) and 1700000000 < v < 1800000000:
            date_fields.append(k)
        elif isinstance(v, str) and ("202" in str(v) or "T" in str(v)):
            date_fields.append(k)
    print(f"Date-like fields: {date_fields}")
    
    # Search using found date fields
    yesterday_files = []
    for f in files:
        for dk in date_fields:
            val = f.get(dk, 0)
            if isinstance(val, (int, float)) and yesterday_ts_start <= val < yesterday_ts_end:
                yesterday_files.append(f)
                break
            elif isinstance(val, str) and yesterday in val:
                yesterday_files.append(f)
                break
    
    print(f"\nFiles from yesterday: {len(yesterday_files)}")
    for f in yesterday_files:
        print(f"  {f.get('Title', f.get('title', '?'))} - {f.get('EpisodeTitle', f.get('episode_title', ''))}")
        for dk in date_fields:
            val = f.get(dk, "")
            if isinstance(val, (int, float)) and val > 1700000000:
                print(f"    {dk}: {val} ({datetime.fromtimestamp(val).isoformat()})")
            elif val:
                print(f"    {dk}: {val}")
        fpath = f.get("Path", f.get("path", ""))
        if fpath:
            print(f"    Path: {fpath}")
        print()
    
    if not yesterday_files:
        # Show last 5 by any date field
        print("\nLast 5 by first date field:")
        dk = date_fields[0] if date_fields else None
        if dk:
            sorted_files = sorted(files, key=lambda x: x.get(dk, 0), reverse=True)
            for f in sorted_files[:5]:
                val = f.get(dk, 0)
                ts = datetime.fromtimestamp(val).isoformat() if isinstance(val, (int, float)) and val > 1700000000 else val
                print(f"  {ts}: {f.get('Title', f.get('title', '?'))} - {f.get('EpisodeTitle', f.get('episode_title', ''))}")
