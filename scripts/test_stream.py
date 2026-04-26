#!/usr/bin/env python3
"""Test the streaming endpoint directly, with and without explicit systems."""
import json, sys, time, urllib.request

BASE = "http://127.0.0.1:8000/api/query/stream"

def test_stream(prompt, systems=None):
    body = {"prompt": prompt}
    if systems:
        body["systems"] = systems
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    resp = urllib.request.urlopen(req, timeout=60)
    tokens = []
    result = None
    for raw_line in resp:
        line = raw_line.decode().strip()
        if not line.startswith("data: "):
            continue
        evt = json.loads(line[6:])
        if evt.get("type") == "status":
            print(f"  status: {evt['message']}")
        elif evt.get("type") == "token":
            tokens.append(evt["token"])
        elif evt.get("type") == "result":
            result = evt["data"]
    elapsed = time.time() - t0
    answer = result.get("llm_response", "") if result else ""
    print(f"  Time: {elapsed:.1f}s  Tokens: {len(tokens)}  Response ({len(answer)} chars): {answer[:120]}")
    return answer

print("=== Test 1: streaming WITHOUT explicit systems ===")
test_stream("what records sunday")

print("\n=== Test 2: streaming WITH systems=['sagetv','channelsdvr'] ===")
test_stream("what records sunday", systems=["sagetv", "channelsdvr"])
