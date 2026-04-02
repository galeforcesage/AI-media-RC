#!/usr/bin/env python3
"""Quick test: list tools + resources from MCP SageTV server."""
import socket, json

def query(method, params=None):
    s = socket.create_connection(("127.0.0.1", 8766))
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}) + "\n"
    s.sendall(req.encode())
    data = b""
    while b"\n" not in data:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data.decode().strip())

# List tools
r = query("tools/list")
tools = r["result"]["tools"]
print(f"Total tools: {len(tools)}")
for t in tools:
    print(f"  {t['name']}")

print()

# List resources
r = query("resources/list")
resources = r["result"]["resources"]
print(f"Total resources: {len(resources)}")
for res in resources:
    print(f"  {res['uri']}  ({res['name']})")
