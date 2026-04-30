#!/usr/bin/env python3
"""Quick test: verify validation + entity_count fields in REQUEST_TRACE."""
import json, subprocess, time, sys

API = "http://127.0.0.1:8000/api/query"

def query(prompt):
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", API,
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"prompt": prompt})],
        capture_output=True, text=True, timeout=120,
    )
    return json.loads(r.stdout)

print("=== Test 1: what recorded yesterday ===")
res = query("what recorded yesterday")
print(f"Status: {res.get('status')}")
print(f"Response: {res.get('llm_response', res.get('response', ''))[:200]}")
print()

# Check log for new fields
print("=== Checking REQUEST_TRACE for validation + entity_count ===")
with open("/tmp/orchestrator.log") as f:
    for line in f:
        if "REQUEST_TRACE" not in line:
            continue
        idx = line.find("REQUEST_TRACE ")
        if idx < 0:
            continue
        try:
            t = json.loads(line[idx + 14:])
        except Exception:
            continue
        has_validation = "validation" in t
        has_entity = "entity_count" in t
        print(f"  trace={t['trace_id']}  validation={'✓ '+t.get('validation','?') if has_validation else '✗ MISSING'}  entity_count={'✓ '+str(t.get('entity_count','?')) if has_entity else '✗ MISSING'}  status={t['status']}")

print("\n=== Test 2: what records tonight (follow-up to test entity carry-over) ===")
res2 = query("what records tonight")
print(f"Status: {res2.get('status')}")
print(f"Response: {res2.get('llm_response', res2.get('response', ''))[:200]}")
print()

# Final trace check
print("=== Final trace check ===")
with open("/tmp/orchestrator.log") as f:
    for line in f:
        if "REQUEST_TRACE" not in line:
            continue
        idx = line.find("REQUEST_TRACE ")
        if idx < 0:
            continue
        try:
            t = json.loads(line[idx + 14:])
        except Exception:
            continue
        has_validation = "validation" in t
        has_entity = "entity_count" in t
        print(f"  trace={t['trace_id']}  validation={'✓ '+t.get('validation','?') if has_validation else '✗ MISSING'}  entity_count={'✓ '+str(t.get('entity_count','?')) if has_entity else '✗ MISSING'}  q={t['query'][:40]}")

# Check entity store log
print("\n=== Entity extraction log entries ===")
with open("/tmp/orchestrator.log") as f:
    for line in f:
        if "EntityContextStore" in line:
            print(f"  {line.strip()}")
