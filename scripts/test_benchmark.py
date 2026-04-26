#!/usr/bin/env python3
"""Benchmark hermes3:8b vs qwen2.5:latest on tool calling."""
import json, urllib.request, time

def test_model(model, prompt, tools=None, n=3):
    results = []
    for i in range(n):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        t0 = time.time()
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read())
        elapsed = time.time() - t0
        content = result.get("message", {}).get("content", "")
        tc = result.get("message", {}).get("tool_calls", [])
        eval_count = result.get("eval_count", 0)
        eval_dur = result.get("eval_duration", 1) / 1e9  # nanoseconds to seconds
        tps = eval_count / eval_dur if eval_dur > 0 else 0
        results.append({
            "time": elapsed,
            "eval_count": eval_count,
            "tps": tps,
            "content_len": len(content),
            "tool_calls": len(tc),
            "content_preview": content[:80],
        })
        tool_names = [t["function"]["name"] + "(" + json.dumps(t["function"].get("arguments", {})) + ")" for t in tc]
        print(f"  Run {i+1}: {elapsed:.1f}s, {tps:.0f} tok/s, {eval_count} tokens, "
              f"tools={len(tc)}{' -> ' + ', '.join(tool_names) if tc else ''}, "
              f"content({len(content)}): {content[:60]}")
    return results

# Real tool schemas (simplified from orchestrator)
tools = [
    {"type": "function", "function": {"name": "get_upcoming_recordings", "description": "Get upcoming/scheduled recordings from SageTV", "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "Date in YYYY-MM-DD format"}}, "required": ["date"]}}},
    {"type": "function", "function": {"name": "get_recordings", "description": "Get recorded shows from SageTV", "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "Date in YYYY-MM-DD format"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_scheduled_recordings", "description": "Get scheduled recordings from Channels DVR", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "search_recordings", "description": "Search recorded content by title or keyword", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search term"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "get_system_status", "description": "Get system status and health info", "parameters": {"type": "object", "properties": {}, "required": []}}},
]

for model in ["hermes3:8b", "qwen2.5:latest"]:
    print(f"\n{'='*60}")
    print(f"Model: {model}")
    print(f"{'='*60}")

    print(f"\n--- Test 1: Simple chat (no tools) ---")
    test_model(model, "Say hello in one sentence.")

    print(f"\n--- Test 2: Tool call with 5 tools ---")
    test_model(model, "What records on Sunday 2026-04-26?", tools=tools)

    print(f"\n--- Test 3: Past query with tools ---")
    test_model(model, "What recorded last Sunday 2026-04-19?", tools=tools)
