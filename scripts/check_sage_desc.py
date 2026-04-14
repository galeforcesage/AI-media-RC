#!/usr/bin/env python3
"""Check SageTV show descriptions for Happy's Place."""
import json, urllib.request, base64

url = "http://localhost:8080/sagex/api?c=GetMediaFiles&1=T&encoder=json"
req = urllib.request.Request(url)
auth = base64.b64encode(b"sage:frey").decode()
req.add_header("Authorization", f"Basic {auth}")
resp = urllib.request.urlopen(req)
data = json.loads(resp.read())
result = data.get("Result", data)
items = result if isinstance(result, list) else [result]

for mf in items:
    airing = mf.get("Airing", {})
    show = airing.get("Show", {})
    if "Happy" in show.get("ShowTitle", ""):
        print("=== Show keys ===")
        print(sorted(show.keys()))
        print()
        print("=== Show fields ===")
        print(json.dumps(show, indent=2, default=str))
        break
else:
    print("No Happy's Place found in SageTV")
