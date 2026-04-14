#!/usr/bin/env python3
"""Test the /api/query endpoint with new config-driven params."""
import json, urllib.request, sys, time

url = "http://127.0.0.1:8000/api/query"
payload = {"prompt": "What was recorded yesterday?", "systems": ["channelsdvr"]}
data = json.dumps(payload).encode()
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

print(f"Sending query: {payload}")
start = time.time()
try:
    resp = urllib.request.urlopen(req, timeout=300)
    result = json.loads(resp.read().decode())
    elapsed = time.time() - start
    print(f"\nResponse ({elapsed:.1f}s):")
    print(json.dumps(result, indent=2)[:1000])
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    elapsed = time.time() - start
    print(f"Error after {elapsed:.1f}s: {e}", file=sys.stderr)
    sys.exit(1)
