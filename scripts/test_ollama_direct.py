#!/usr/bin/env python3
"""Direct Ollama test - bypass orchestrator entirely."""
import json, urllib.request, time

# Test 1: Simple chat, no tools
print("=== Test 1: Simple chat, no tools ===")
body = {
    "model": "qwen3:8b",
    "messages": [{"role": "user", "content": "Say hello in one sentence."}],
    "stream": False,
    "think": False,
}
t0 = time.time()
req = urllib.request.Request(
    "http://127.0.0.1:11434/api/chat",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
resp = urllib.request.urlopen(req, timeout=60)
result = json.loads(resp.read())
content = result.get("message", {}).get("content", "")
eval_count = result.get("eval_count", 0)
print(f"  Time: {time.time()-t0:.1f}s  eval_count: {eval_count}  Content: {content[:200]}")

# Test 2: Chat with tools (like the orchestrator sends)
print("\n=== Test 2: Chat with 2 simple tools ===")
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_recordings",
            "description": "Get scheduled recordings",
            "parameters": {
                "type": "object",
                "properties": {"date": {"type": "string", "description": "Date in YYYY-MM-DD"}},
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "Get system status",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
body2 = {
    "model": "qwen3:8b",
    "messages": [{"role": "user", "content": "What records on Sunday 2026-04-26?"}],
    "tools": tools,
    "stream": False,
    "think": False,
}
t0 = time.time()
req2 = urllib.request.Request(
    "http://127.0.0.1:11434/api/chat",
    data=json.dumps(body2).encode(),
    headers={"Content-Type": "application/json"},
)
resp2 = urllib.request.urlopen(req2, timeout=60)
result2 = json.loads(resp2.read())
content2 = result2.get("message", {}).get("content", "")
tool_calls = result2.get("message", {}).get("tool_calls", [])
eval_count2 = result2.get("eval_count", 0)
print(f"  Time: {time.time()-t0:.1f}s  eval_count: {eval_count2}  Content: {content2[:100]}  tool_calls: {len(tool_calls)}")
if tool_calls:
    for tc in tool_calls:
        fn = tc.get("function", {})
        print(f"    -> {fn.get('name')}({fn.get('arguments', {})})")

# Test 3: Chat with MANY tools (33 like the orchestrator)
print("\n=== Test 3: Chat with 33 tools (simulated) ===")
many_tools = []
for i in range(33):
    many_tools.append({
        "type": "function",
        "function": {
            "name": f"tool_{i}",
            "description": f"Tool number {i} does something",
            "parameters": {"type": "object", "properties": {"arg": {"type": "string"}}, "required": []},
        },
    })
body3 = {
    "model": "qwen3:8b",
    "messages": [{"role": "user", "content": "What records on Sunday 2026-04-26?"}],
    "tools": many_tools,
    "stream": False,
    "think": False,
}
t0 = time.time()
req3 = urllib.request.Request(
    "http://127.0.0.1:11434/api/chat",
    data=json.dumps(body3).encode(),
    headers={"Content-Type": "application/json"},
)
resp3 = urllib.request.urlopen(req3, timeout=60)
result3 = json.loads(resp3.read())
content3 = result3.get("message", {}).get("content", "")
tool_calls3 = result3.get("message", {}).get("tool_calls", [])
eval_count3 = result3.get("eval_count", 0)
print(f"  Time: {time.time()-t0:.1f}s  eval_count: {eval_count3}  Content ({len(content3)} chars): {content3[:100]}  tool_calls: {len(tool_calls3)}")
if tool_calls3:
    for tc in tool_calls3:
        fn = tc.get("function", {})
        print(f"    -> {fn.get('name')}({fn.get('arguments', {})})")
