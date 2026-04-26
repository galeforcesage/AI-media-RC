#!/usr/bin/env python3
"""Quick e2e test: what records on sunday?"""
import json, time, urllib.request

url = "http://127.0.0.1:8000/api/query"
payload = json.dumps({"text": "what records on sunday?", "prompt": "what records on sunday?"}).encode()
req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    print(f"Time: {elapsed:.1f}s")
    print(f"LLM response ({len(data.get('llm_response', data.get('response','')))} chars):")
    print(data.get("llm_response", data.get("response", "(empty)")))
    if data.get("tool_results"):
        print(f"\nTool results: {len(data['tool_results'])} calls")
        for tr in data["tool_results"]:
            print(f"  - {tr.get('tool','?')}: {str(tr.get('result',''))[:100]}")
except urllib.error.HTTPError as e:
    elapsed = time.time() - t0
    body = e.read().decode()
    print(f"HTTP {e.code} after {elapsed:.1f}s: {body[:500]}")
except Exception as e:
    elapsed = time.time() - t0
    print(f"Error after {elapsed:.1f}s: {e}")
