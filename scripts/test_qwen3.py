#!/usr/bin/env python3
"""Test qwen3:8b tool calling via Ollama API."""
import urllib.request, json, time

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:8b"

def api_call(endpoint, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{OLLAMA}{endpoint}", data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())

# Test 1: Basic speed
print("=== Test 1: Basic inference ===")
start = time.time()
r = api_call("/api/generate", {"model": MODEL, "prompt": "Say hello in one sentence", "stream": False})
elapsed = time.time() - start
eval_ns = r.get("eval_duration", 0)
eval_count = r.get("eval_count", 0)
print(f"Response: {r.get('response', '')[:200]}")
print(f"Time: {elapsed:.1f}s, Tokens: {eval_count}, Speed: {eval_count/(eval_ns/1e9):.1f} tok/s" if eval_ns else f"Time: {elapsed:.1f}s")

# Test 2: Tool calling
print("\n=== Test 2: Tool calling ===")
tools = [{
    "type": "function",
    "function": {
        "name": "channels_search_recordings",
        "description": "Search recordings on Channels DVR",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Show name to search for"},
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
            },
        },
    },
}]
messages = [
    {"role": "system", "content": "You are a home media assistant. Today is 2026-04-25. Use tools to answer questions about recordings."},
    {"role": "user", "content": "What recorded last Thursday?"},
]
start = time.time()
r = api_call("/api/chat", {"model": MODEL, "messages": messages, "tools": tools, "stream": False})
elapsed = time.time() - start
msg = r.get("message", {})
print(f"Role: {msg.get('role')}")
print(f"Content: {msg.get('content', '')[:300]}")
print(f"Tool calls: {json.dumps(msg.get('tool_calls', []), indent=2)[:500]}")
print(f"Time: {elapsed:.1f}s")

# Test 3: Check thinking tags
print("\n=== Test 3: Thinking mode check ===")
content = msg.get("content", "")
if "<think>" in content:
    print("WARNING: Model is using <think> tags — need to strip them")
else:
    print("OK: No <think> tags in tool-call response")

# Check processor
import subprocess
result = subprocess.run(["ollama", "ps"], capture_output=True, text=True)
print(f"\n=== GPU Status ===\n{result.stdout}")
