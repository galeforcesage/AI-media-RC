#!/usr/bin/env python3
"""Direct Ollama API test for hermes3:8b with tools — bypasses our code entirely."""
import json, time, requests

OLLAMA = "http://127.0.0.1:11434/api/chat"

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_scheduled_recordings",
            "description": "Get upcoming scheduled recordings from Channels DVR",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_epg",
            "description": "Search the EPG for shows matching a query",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        }
    }
]

messages = [
    {"role": "system", "content": "You are a helpful assistant that controls a home media system. Use the available tools to answer questions. Today is Friday, April 25, 2025."},
    {"role": "user", "content": "What is recording this Sunday?"}
]

print("=" * 60)
print("Direct Ollama API test — hermes3:8b with tools")
print("=" * 60)

for i in range(5):
    t0 = time.time()
    
    # Test with stream=false first
    resp = requests.post(OLLAMA, json={
        "model": "hermes3:8b",
        "stream": False,
        "messages": messages,
        "tools": tools,
        "options": {"temperature": 0.7, "num_predict": 512}
    })
    elapsed = time.time() - t0
    data = resp.json()
    
    msg = data.get("message", {})
    content = msg.get("content", "")
    tool_calls = msg.get("tool_calls", [])
    eval_count = data.get("eval_count", 0)
    
    status = "TOOL_CALL" if tool_calls else ("EMPTY" if not content else "TEXT")
    print(f"\nRun {i+1}: {status} | {elapsed:.1f}s | eval={eval_count} | content={repr(content[:80])} | tools={len(tool_calls)}")
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            print(f"  -> {fn.get('name')}({json.dumps(fn.get('arguments', {}))})")

# Now test with stream=true to match our code path
print("\n" + "=" * 60)
print("Streaming mode test (3 runs)")
print("=" * 60)

for i in range(3):
    t0 = time.time()
    resp = requests.post(OLLAMA, json={
        "model": "hermes3:8b",
        "stream": True,
        "messages": messages,
        "tools": tools,
        "options": {"temperature": 0.7, "num_predict": 512}
    }, stream=True)
    
    chunks = []
    full_content = ""
    tool_calls = []
    eval_count = 0
    
    for line in resp.iter_lines():
        if line:
            chunk = json.loads(line)
            chunks.append(chunk)
            msg = chunk.get("message", {})
            if msg.get("content"):
                full_content += msg["content"]
            if msg.get("tool_calls"):
                tool_calls.extend(msg["tool_calls"])
            if chunk.get("done"):
                eval_count = chunk.get("eval_count", 0)
    
    elapsed = time.time() - t0
    status = "TOOL_CALL" if tool_calls else ("EMPTY" if not full_content else "TEXT")
    print(f"\nStream {i+1}: {status} | {elapsed:.1f}s | eval={eval_count} | chunks={len(chunks)} | content={repr(full_content[:80])} | tools={len(tool_calls)}")
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            print(f"  -> {fn.get('name')}({json.dumps(fn.get('arguments', {}))})")

print("\nDone.")
