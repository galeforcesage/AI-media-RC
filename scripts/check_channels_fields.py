#!/usr/bin/env python3
"""Quick script to inspect raw Channels DVR fields for a recording."""
import json, urllib.request

resp = urllib.request.urlopen("http://localhost:8089/dvr/files")
data = json.loads(resp.read())

for r in data:
    airing = r.get("Airing", {})
    if "Happy" in airing.get("Title", ""):
        print("=== Airing keys ===")
        print(sorted(airing.keys()))
        print()
        print("=== Full Airing ===")
        print(json.dumps(airing, indent=2, default=str))
        break
