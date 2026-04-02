#!/usr/bin/env python3
"""Quick test script for MCP SageTV server."""
import asyncio
import json
import sys


async def send_rpc(method, params=None, host="127.0.0.1", port=8766):
    reader, writer = await asyncio.open_connection(host, port)
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(line.decode())


async def main():
    print("=== Testing MCP SageTV Server ===\n")

    # 1. Initialize
    print("1. initialize")
    r = await send_rpc("initialize")
    print(json.dumps(r, indent=2))
    print()

    # 2. List tools
    print("2. tools/list")
    r = await send_rpc("tools/list")
    tools = r.get("result", {}).get("tools", [])
    print(f"   {len(tools)} tools registered")
    for t in tools[:5]:
        print(f"   - {t['name']}: {t.get('description','')[:60]}")
    if len(tools) > 5:
        print(f"   ... and {len(tools)-5} more")
    print()

    # 3. List resources
    print("3. resources/list")
    r = await send_rpc("resources/list")
    resources = r.get("result", {}).get("resources", [])
    print(f"   {len(resources)} resources")
    for res in resources:
        print(f"   - {res['uri']}: {res.get('name','')}")
    print()

    # 4. Ping
    print("4. ping")
    r = await send_rpc("ping")
    print(json.dumps(r, indent=2))
    print()

    # 5. Call a safe tool — get_channels
    print("5. tools/call sagetv_get_channels")
    r = await send_rpc("tools/call", {"name": "sagetv_get_channels", "arguments": {}})
    print(json.dumps(r, indent=2)[:500])
    print()

    # 6. Call sagetv_get_disk_space
    print("6. tools/call sagetv_get_disk_space")
    r = await send_rpc("tools/call", {"name": "sagetv_get_disk_space", "arguments": {}})
    print(json.dumps(r, indent=2)[:500])
    print()

    # 7. Read resource — system status
    print("7. resources/read sagetv://system/status")
    r = await send_rpc("resources/read", {"uri": "sagetv://system/status"})
    print(json.dumps(r, indent=2)[:500])
    print()

    print("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
