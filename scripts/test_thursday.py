#!/usr/bin/env python3
import json, sys, urllib.request

data = json.dumps({"prompt": "what recorded last Thursday? Do they have transcripts?"}).encode()
req = urllib.request.Request(
    "http://localhost:8000/api/query",
    data=data,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=180)
result = json.loads(resp.read())
print(json.dumps(result, indent=2)[:3000])
