#!/usr/bin/env python3
"""End-to-end test: playback status through orchestrator → MCP SageTV."""
import urllib.request
import json

# Test 1: Playback status (goes through orchestrator → MCP SageTV)
payload = json.dumps({"action": "status", "target": "sagetv"}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8000/api/playback",
    data=payload,
    headers={"Content-Type": "application/json"},
)
try:
    resp = urllib.request.urlopen(req, timeout=10)
    print("1. Playback status:", resp.read().decode())
except Exception as e:
    print("1. Playback status ERROR:", e)
    if hasattr(e, 'read'):
        print("   Body:", e.read().decode())

# Test 2: Health check
req2 = urllib.request.Request("http://127.0.0.1:8000/api/health")
try:
    resp2 = urllib.request.urlopen(req2, timeout=5)
    print("2. Health:", resp2.read().decode())
except Exception as e:
    print("2. Health ERROR:", e)

# Test 3: Playback pause with device_id (tests session resolution path)
payload3 = json.dumps({
    "action": "pause",
    "target": "sagetv",
    "device_id": "test-device-123",
    "payload": {},
}).encode()
req3 = urllib.request.Request(
    "http://127.0.0.1:8000/api/playback",
    data=payload3,
    headers={"Content-Type": "application/json"},
)
try:
    resp3 = urllib.request.urlopen(req3, timeout=10)
    print("3. Pause w/ device_id:", resp3.read().decode())
except Exception as e:
    print("3. Pause w/ device_id ERROR:", e)
    if hasattr(e, 'read'):
        print("   Body:", e.read().decode())
