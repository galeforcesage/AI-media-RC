#!/usr/bin/env python3
"""Test the end-to-end LLM query flow with transcript context."""
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
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()}

def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read())

# 1. Health
print("=== Health ===")
print(json.dumps(get("/health"), indent=2))

# 2. Query (should go through LLM with transcript context injection)
print("\n=== Query: 'What episodes mention climate change?' ===")
result = post("/query", {"prompt": "What episodes mention climate change?", "synthesize": False})
print(json.dumps(result, indent=2))

# 3. Search (should search both programs and transcripts)
print("\n=== Search: 'breaking bad' ===")
result = get("/search?q=breaking+bad")
print(json.dumps(result, indent=2))

# 4. Query about actors
print("\n=== Query: 'Find recordings with Bryan Cranston' ===")
result = post("/query", {"prompt": "Find recordings with Bryan Cranston", "synthesize": False})
print(json.dumps(result, indent=2))

print("\n=== All tests complete ===")
