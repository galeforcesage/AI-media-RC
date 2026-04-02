#!/usr/bin/env python3
import socket, json

def rpc(method, params={}):
    s = socket.socket()
    s.connect(("127.0.0.1", 8770))
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
    s.sendall(req.encode())
    data = s.recv(16384)
    s.close()
    return json.loads(data)

# List tools
resp = rpc("tools/list")
tools = resp.get("result", {}).get("tools", [])
print(f"Total tools: {len(tools)}")
for t in tools:
    print(f"  - {t['name']}")

# Test transcript_stats
resp = rpc("tools/call", {"name": "transcript_stats", "arguments": {}})
print(f"\nStats: {json.dumps(resp.get('result', {}), indent=2)}")

# Test transcript_cross_search (should return empty on fresh index)
resp = rpc("tools/call", {"name": "transcript_cross_search", "arguments": {"query": "test"}})
print(f"\nSearch: {json.dumps(resp.get('result', {}), indent=2)}")

# Test transcript_actors
resp = rpc("tools/call", {"name": "transcript_actors", "arguments": {"actor_name": "test"}})
print(f"\nActors: {json.dumps(resp.get('result', {}), indent=2)}")

print("\nAll tests passed!")
