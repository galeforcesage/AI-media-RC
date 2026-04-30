#!/usr/bin/env python3
"""Test all 8 PRD improvements on the live orchestrator."""
import requests
import json
import time

BASE = "http://127.0.0.1:8000/api"

def test_query(prompt, label=""):
    """Run a query and print results."""
    print(f"\n{'='*60}")
    print(f"TEST: {label or prompt}")
    print(f"Prompt: {prompt}")
    start = time.time()
    try:
        r = requests.post(f"{BASE}/query", json={"prompt": prompt}, timeout=120)
        elapsed = time.time() - start
        d = r.json()
        status = d.get("status", "?")
        response = d.get("llm_response") or d.get("response", "")
        iterations = d.get("iterations", "?")
        error = d.get("error")
        print(f"Status: {status}")
        print(f"Iterations: {iterations}")
        print(f"Time: {elapsed:.1f}s")
        if error:
            print(f"Error: {error}")
        print(f"Response: {response[:400]}")
        # Check for confirmation
        if d.get("confirmation"):
            print(f"CONFIRMATION REQUIRED: {d['confirmation']}")
        return d
    except Exception as e:
        print(f"EXCEPTION: {e}")
        return {"error": str(e)}

# ── Test 1: Basic past query (domain=recordings, temporal=past)
print("\n" + "#"*60)
print("# TEST SUITE: PRD Improvements Validation")
print("#"*60)

test_query("what recorded yesterday", "Past query - domain subsetting + temporal filtering")

# ── Test 2: Future query (schedule domain)
test_query("what records tonight", "Future query - schedule domain")

# ── Test 3: System query (system domain)  
test_query("how much disk space is left", "System query - system domain only")

# ── Test 4: Metadata query
test_query("what genres are available", "Metadata query - metadata domain")

# ── Test 5: Check REQUEST_TRACE in logs
print("\n" + "="*60)
print("TEST: Checking for REQUEST_TRACE in orchestrator log")
import subprocess
result = subprocess.run(
    ["grep", "-c", "REQUEST_TRACE", "/tmp/orchestrator.log"],
    capture_output=True, text=True
)
count = result.stdout.strip()
print(f"REQUEST_TRACE entries found: {count}")

# Show the last trace
result2 = subprocess.run(
    ["grep", "REQUEST_TRACE", "/tmp/orchestrator.log"],
    capture_output=True, text=True
)
lines = result2.stdout.strip().split("\n")
if lines and lines[-1]:
    # Extract just the JSON part
    last_line = lines[-1]
    idx = last_line.find("REQUEST_TRACE ")
    if idx >= 0:
        trace_json = last_line[idx + len("REQUEST_TRACE "):]
        try:
            trace = json.loads(trace_json)
            print(f"Last trace:")
            print(f"  trace_id: {trace.get('trace_id')}")
            print(f"  query: {trace.get('query')}")
            print(f"  temporal: {trace.get('temporal')}")
            print(f"  domains: {trace.get('domains')}")
            print(f"  tools_offered: {trace.get('tools_offered')}")
            print(f"  steps: {len(trace.get('steps', []))} tool calls")
            for s in trace.get("steps", []):
                print(f"    - {s['tool']}({s['args_keys']}) {s['duration_ms']}ms, {s['result_size']} bytes")
            print(f"  iterations: {trace.get('iterations')}")
            print(f"  total_ms: {trace.get('total_ms'):.0f}ms")
            print(f"  status: {trace.get('status')}")
            print(f"  context_tokens_est: {trace.get('context_tokens_est')}")
            if trace.get("validation_issues"):
                print(f"  validation_issues: {trace['validation_issues']}")
        except json.JSONDecodeError:
            print(f"  (could not parse trace JSON)")

print("\n" + "#"*60)
print("# ALL TESTS COMPLETE")
print("#"*60)
