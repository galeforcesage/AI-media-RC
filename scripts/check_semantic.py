#!/usr/bin/env python3
"""Check what semantic context the LLM sees for a 'recorded yesterday' query."""
import json, urllib.request

# Query the semantic index via the search endpoint
resp = urllib.request.urlopen(
    urllib.request.Request(
        "http://127.0.0.1:8000/api/search?query=what+was+recorded+yesterday&target=semantic",
        headers={"Content-Type": "application/json"},
    ),
    timeout=30,
)
result = json.loads(resp.read().decode())
print("Semantic search results:")
print(json.dumps(result, indent=2, default=str)[:2000])
