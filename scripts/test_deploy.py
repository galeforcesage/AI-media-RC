#!/usr/bin/env python3
"""Quick deploy test — sends a query and prints the response."""
import urllib.request, json, sys

data = json.dumps({"prompt": "what shows are supposed to record tomorrow", "session_id": "test-prompt-size"}).encode()
req = urllib.request.Request(
    "http://localhost:8000/api/query",
    data=data,
    headers={"Content-Type": "application/json"},
)
try:
    resp = urllib.request.urlopen(req, timeout=120)
    body = json.loads(resp.read().decode())
    print(json.dumps(body, indent=2))
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
    sys.exit(1)
