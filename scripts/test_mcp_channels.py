#!/usr/bin/env python3
"""Quick test script for MCP Channels DVR server."""
import asyncio
import json


async def send_rpc(method, params=None, host="127.0.0.1", port=8767):
    reader, writer = await asyncio.open_connection(host, port)
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(line.decode())


async def main():
    print("=== Testing MCP Channels DVR Server ===\n")

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

    # 5. Get storage status
    print("5. tools/call channels_get_storage_status")
    r = await send_rpc("tools/call", {"name": "channels_get_storage_status", "arguments": {}})
    print(json.dumps(r, indent=2)[:500])
    print()

    # 6. Get channels
    print("6. tools/call channels_get_channels")
    r = await send_rpc("tools/call", {"name": "channels_get_channels", "arguments": {}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} message={data.get('message')}")
    print()

    # 7. Get recordings (limit 3)
    print("7. tools/call channels_get_recordings (limit=3)")
    r = await send_rpc("tools/call", {"name": "channels_get_recordings", "arguments": {"limit": 3}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} message={data.get('message')}")
        for rec in data.get("data", [])[:3]:
            title = rec.get("Title", rec.get("title", "?"))
            print(f"   - {title}")
    print()

    # 8. Read resource — system status
    print("8. resources/read channels://system/status")
    r = await send_rpc("resources/read", {"uri": "channels://system/status"})
    contents = r.get("result", {}).get("contents", [])
    if contents:
        data = json.loads(contents[0].get("text", "{}"))
        print(f"   version={data.get('version')} name={data.get('name')} os={data.get('os')}")
    print()

    # 9. Read resource — storage
    print("9. resources/read channels://storage")
    r = await send_rpc("resources/read", {"uri": "channels://storage"})
    contents = r.get("result", {}).get("contents", [])
    if contents:
        data = json.loads(contents[0].get("text", "{}"))
        free_gb = data.get("free", 0) / (1024**3)
        total_gb = data.get("total", 0) / (1024**3)
        print(f"   free={free_gb:.1f}GB total={total_gb:.1f}GB")
    print()

    print("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
