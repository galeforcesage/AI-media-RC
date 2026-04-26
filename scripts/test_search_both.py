#!/usr/bin/env python3
"""Test channels_search_recordings MCP call for April 19, 2026."""
import json, socket

def mcp_call(port, method, params):
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
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

# Call channels_search_recordings
result = mcp_call(8767, "channels_search_recordings", {
    "start_date": "2026-04-19",
    "end_date": "2026-04-19"
})
print("channels_search_recordings result:")
if "result" in result:
    r = result["result"]
    if isinstance(r, list):
        print(f"  Got {len(r)} items")
        for item in r[:10]:
            title = item.get("title", item.get("Title", "?"))
            print(f"  - {title}")
    elif isinstance(r, dict):
        items = r.get("recordings", r.get("results", []))
        print(f"  Got {len(items)} items")
        for item in items[:10]:
            title = item.get("title", item.get("Title", "?"))
            ep = item.get("episode_title", item.get("EpisodeTitle", ""))
            print(f"  - {title} {ep}")
        if not items:
            print(f"  Raw keys: {list(r.keys())}")
            print(f"  Raw: {json.dumps(r)[:500]}")
    else:
        print(f"  Raw: {str(r)[:500]}")
elif "error" in result:
    print(f"  Error: {result['error']}")
else:
    print(f"  Unknown: {json.dumps(result)[:500]}")

# Also test sagetv_search_recordings 
result2 = mcp_call(8766, "sagetv_search_recordings", {
    "start_date": "2026-04-19",
    "end_date": "2026-04-19"
})
print("\nsagetv_search_recordings result:")
if "result" in result2:
    r = result2["result"]
    if isinstance(r, list):
        print(f"  Got {len(r)} items")
        for item in r[:10]:
            title = item.get("title", item.get("Title", "?"))
            print(f"  - {title}")
    elif isinstance(r, dict):
        items = r.get("recordings", r.get("results", []))
        print(f"  Got {len(items)} items")
        for item in items[:10]:
            title = item.get("title", item.get("Title", "?"))
            ep = item.get("episode_title", item.get("EpisodeTitle", ""))
            print(f"  - {title} {ep}")
        if not items:
            print(f"  Raw keys: {list(r.keys())}")
            print(f"  Raw: {json.dumps(r)[:500]}")
    else:
        print(f"  Raw: {str(r)[:500]}")
elif "error" in result2:
    print(f"  Error: {result2['error']}")
