#!/usr/bin/env python3
"""Quick test: send playback commands through the orchestrator to a bridge device."""
import requests, json, sys

BASE = "http://localhost:8000/api"
S = requests
DEVICE = "SHIELD Pro Android TV"

# 1. Check bridge devices
print("=== Bridge Devices ===")
r = requests.get(f"{BASE}/bridge/devices")
print(r.status_code, r.text)

# 2. Get playback status via bridge (direct MCP route)
print("\n=== Bridge Status (direct) ===")
r = requests.get(f"{BASE}/bridge/status", params={"device": DEVICE})
print(r.status_code, json.dumps(r.json(), indent=2))

# 3. Get playback status via /playback endpoint
print("\n=== Playback Status ===")
r = requests.post(f"{BASE}/playback", json={
    "action": "status",
    "target": "channelsdvr",
    "payload": {"device": DEVICE}
})
print(r.status_code, json.dumps(r.json(), indent=2))

# 4. Toggle pause
if "--toggle" in sys.argv:
    print("\n=== Toggle Pause ===")
    r = requests.post(f"{BASE}/playback", json={
        "action": "play_pause",
        "target": "channelsdvr",
        "payload": {"device": DEVICE}
    })
    print(r.status_code, json.dumps(r.json(), indent=2))
