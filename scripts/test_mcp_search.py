#!/usr/bin/env python3
"""Test MCP tools/call for both search tools."""
import json, socket

def mcp_call(port, tool_name, args):
    msg = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args}
    })
    with socket.create_connection(("127.0.0.1", port), timeout=30) as s:
        s.sendall(msg.encode() + b"\n")
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
    return json.loads(data.split(b"\n")[0])

# Test channels
print("=== channels_search_recordings (port 8767) ===")
r = mcp_call(8767, "channels_search_recordings", {
    "start_date": "2026-04-19", "end_date": "2026-04-19"
})
if "result" in r:
    content = r["result"].get("content", [])
    if content:
        text = content[0].get("text", "")
        try:
            data = json.loads(text)
            if isinstance(data, list):
                print(f"Got {len(data)} items")
                for item in data[:10]:
                    print(f"  - {item.get('title','?')} - {item.get('episode_title','')}")
            elif isinstance(data, dict):
                recs = data.get("recordings", data.get("results", []))
                print(f"Got {len(recs)} recordings")
                for item in recs[:10]:
                    print(f"  - {item.get('title','?')} - {item.get('episode_title','')}")
                if not recs:
                    print(f"Keys: {list(data.keys())}")
                    print(f"Raw: {text[:500]}")
            else:
                print(f"Raw: {text[:500]}")
        except:
            print(f"Raw text: {text[:500]}")
    else:
        print(f"No content: {json.dumps(r['result'])[:500]}")
elif "error" in r:
    print(f"Error: {r['error']}")

# Test sagetv
print("\n=== sagetv_search_recordings (port 8766) ===")
r = mcp_call(8766, "sagetv_search_recordings", {
    "start_date": "2026-04-19", "end_date": "2026-04-19"
})
if "result" in r:
    content = r["result"].get("content", [])
    if content:
        text = content[0].get("text", "")
        try:
            data = json.loads(text)
            if isinstance(data, list):
                print(f"Got {len(data)} items")
                for item in data[:10]:
                    print(f"  - {item.get('title','?')} - {item.get('episode_title','')}")
            elif isinstance(data, dict):
                recs = data.get("recordings", data.get("results", []))
                print(f"Got {len(recs)} recordings")
                for item in recs[:10]:
                    print(f"  - {item.get('title','?')} - {item.get('episode_title','')}")
                if not recs:
                    print(f"Keys: {list(data.keys())}")
                    print(f"Raw: {text[:500]}")
        except:
            print(f"Raw text: {text[:500]}")
    else:
        print(f"No content: {json.dumps(r['result'])[:500]}")
elif "error" in r:
    print(f"Error: {r['error']}")
