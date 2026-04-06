#!/usr/bin/env python3
"""Quick test script for the Unified Session Manager."""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8769"


def get(path):
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def delete(path):
    req = urllib.request.Request(f"{BASE}{path}", method="DELETE")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    print("=== Testing Unified Session Manager ===\n")

    # 1. Health
    print("1. GET /health")
    r = get("/health")
    print(f"   {r}")
    print()

    # 2. List devices (should be empty)
    print("2. GET /devices")
    r = get("/devices")
    print(f"   count={r.get('count')} devices={len(r.get('devices', []))}")
    print()

    # 3. Add SageTV device
    print("3. POST /devices (SageTV Shield)")
    r = post("/devices", {
        "system": "sagetv",
        "friendly_name": "Living Room Shield",
        "ip_address": "192.168.1.100",
        "platform": "shield",
    })
    sagetv_id = r.get("device", {}).get("device_id", "")
    print(f"   success={r.get('success')} device_id={sagetv_id}")
    print()

    # 4. Add Channels DVR device
    print("4. POST /devices (Channels Chromecast)")
    r = post("/devices", {
        "system": "channelsdvr",
        "friendly_name": "Bedroom Chromecast",
        "ip_address": "192.168.1.101",
        "platform": "chromecast",
    })
    channels_id = r.get("device", {}).get("device_id", "")
    print(f"   success={r.get('success')} device_id={channels_id}")
    print()

    # 5. List devices
    print("5. GET /devices")
    r = get("/devices")
    print(f"   count={r.get('count')}")
    for d in r.get("devices", []):
        print(f"   - {d['device_id']}: {d['friendly_name']} ({d['system']})")
    print()

    # 6. Set default
    print(f"6. POST /devices/{sagetv_id}/default")
    r = post(f"/devices/{sagetv_id}/default")
    print(f"   success={r.get('success')}")
    print()

    # 7. Get default
    print("7. GET /devices/default")
    r = get("/devices/default")
    print(f"   default={r.get('device', {}).get('friendly_name')}")
    print()

    # 8. Resolve session for SageTV device
    print(f"8. GET /sessions/resolve/{sagetv_id}")
    r = get(f"/sessions/resolve/{sagetv_id}")
    print(f"   success={r.get('success')} system={r.get('system')}")
    if r.get("session"):
        s = r["session"]
        print(f"   session_id={s.get('session_id')} state={s.get('state')} title={s.get('title')}")
    else:
        print("   (no active session - expected if nothing is playing)")
    print()

    # 9. Resolve default session
    print("9. GET /sessions/resolve")
    r = get("/sessions/resolve")
    print(f"   success={r.get('success')} device_name={r.get('device_name')}")
    print()

    # 10. List all active sessions
    print("10. GET /sessions")
    r = get("/sessions")
    print(f"   count={r.get('count')} sessions")
    for s in r.get("sessions", [])[:3]:
        print(f"   - {s.get('system')}: {s.get('title', 'n/a')} [{s.get('state')}]")
    print()

    # 11. Get specific device
    print(f"11. GET /devices/{sagetv_id}")
    r = get(f"/devices/{sagetv_id}")
    print(f"   success={r.get('success')} name={r.get('device', {}).get('friendly_name')}")
    print()

    # 12. Delete Channels device
    print(f"12. DELETE /devices/{channels_id}")
    r = delete(f"/devices/{channels_id}")
    print(f"   success={r.get('success')}")
    print()

    # 13. Final device list
    print("13. GET /devices (after delete)")
    r = get("/devices")
    print(f"   count={r.get('count')}")
    for d in r.get("devices", []):
        print(f"   - {d['device_id']}: {d['friendly_name']}")
    print()

    # Cleanup — delete the SageTV device too
    delete(f"/devices/{sagetv_id}")

    print("=== Done ===")


if __name__ == "__main__":
    main()
