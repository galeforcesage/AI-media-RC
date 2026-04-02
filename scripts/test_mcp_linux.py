#!/usr/bin/env python3
"""Quick test script for MCP Linux server."""
import asyncio
import json


async def send_rpc(method, params=None, host="127.0.0.1", port=8768):
    reader, writer = await asyncio.open_connection(host, port)
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(line.decode())


async def main():
    print("=== Testing MCP Linux Server ===\n")

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
    for t in tools:
        print(f"   - {t['name']}: {t.get('description','')[:60]}")
    print()

    # 3. List resources
    print("3. resources/list")
    r = await send_rpc("resources/list")
    resources = r.get("result", {}).get("resources", [])
    print(f"   {len(resources)} resources")
    for res in resources:
        print(f"   - {res['uri']}: {res.get('name','')}")
    print()

    # 4. Disk usage
    print("4. tools/call linux_disk_usage")
    r = await send_rpc("tools/call", {"name": "linux_disk_usage", "arguments": {}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} message={data.get('message')}")
        for m in data.get("data", {}).get("mounts", [])[:5]:
            pct = m.get("percent", "?")
            mount = m.get("mount", "?")
            avail_gb = m.get("available", 0) / (1024**3)
            print(f"   - {mount}: {pct} used, {avail_gb:.1f}GB free")
    print()

    # 5. Network info
    print("5. tools/call linux_network_info")
    r = await send_rpc("tools/call", {"name": "linux_network_info", "arguments": {}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} message={data.get('message')}")
    print()

    # 6. Memory info
    print("6. tools/call linux_memory_info")
    r = await send_rpc("tools/call", {"name": "linux_memory_info", "arguments": {}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} message={data.get('message')}")
    print()

    # 7. Uptime
    print("7. tools/call linux_uptime")
    r = await send_rpc("tools/call", {"name": "linux_uptime", "arguments": {}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} message={data.get('message')}")
    print()

    # 8. Docker ps
    print("8. tools/call linux_docker_ps")
    r = await send_rpc("tools/call", {"name": "linux_docker_ps", "arguments": {}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} message={data.get('message')}")
        for c in data.get("data", {}).get("containers", []):
            print(f"   - {c.get('name')}: {c.get('status')}")
    print()

    # 9. Service status (docker)
    print("9. tools/call linux_service_status (docker)")
    r = await send_rpc("tools/call", {"name": "linux_service_status", "arguments": {"service_name": "docker"}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} message={data.get('message')}")
    print()

    # 10. Resource read - disk
    print("10. resources/read linux://system/disk")
    r = await send_rpc("resources/read", {"uri": "linux://system/disk"})
    contents = r.get("result", {}).get("contents", [])
    if contents:
        data = json.loads(contents[0].get("text", "{}"))
        mounts = data.get("mounts", [])
        print(f"   {len(mounts)} mount points")
    print()

    # 11. Disallowed service (should fail)
    print("11. tools/call linux_service_status (nginx - not allowlisted)")
    r = await send_rpc("tools/call", {"name": "linux_service_status", "arguments": {"service_name": "nginx"}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} error={data.get('error')}")
    print()

    print("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
