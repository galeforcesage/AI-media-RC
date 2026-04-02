#!/usr/bin/env python3
"""Verify transcript_results are returned in /api/query response."""
import json
import urllib.request

BASE = "http://localhost:8000/api"

def post(path, data):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

result = post("/query", {"prompt": "What episodes mention climate change?", "synthesize": False})
print(json.dumps(result, indent=2))

print("\n--- Key fields ---")
print(f"Has 'transcript_results': {'transcript_results' in result}")
print(f"Type: {type(result.get('transcript_results'))}")
tr = result.get("transcript_results", [])
print(f"Count: {len(tr)}")
if tr:
    print(f"First result keys: {list(tr[0].keys())}")
