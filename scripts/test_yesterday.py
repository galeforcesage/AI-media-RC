#!/usr/bin/env python3
import json, sys, urllib.request

data = json.dumps({"prompt": "what shows recorded yesterday? Do they have transcripts?"}).encode()
req = urllib.request.Request(
    "http://localhost:8000/api/query",
    data=data,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=120)
result = json.loads(resp.read())
print(json.dumps(result, indent=2)[:3000])
