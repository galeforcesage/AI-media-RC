#!/usr/bin/env python3
"""Quick test script for the Transcription MCP server."""
import asyncio
import json


async def send_rpc(method, params=None, host="127.0.0.1", port=8770):
    reader, writer = await asyncio.open_connection(host, port)
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(line.decode())


async def main():
    print("=== Testing Transcription MCP Server ===\n")

    # 1. Initialize
    print("1. initialize")
    r = await send_rpc("initialize")
    print(json.dumps(r, indent=2))
    print()

    # 2. List tools
    print("2. tools/list")
    r = await send_rpc("tools/list")
    tools = r.get("result", {}).get("tools", [])
    print(f"   {len(tools)} tools")
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

    # 4. Get stats
    print("4. tools/call transcript_stats")
    r = await send_rpc("tools/call", {"name": "transcript_stats", "arguments": {}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   {json.dumps(data, indent=2)}")
    print()

    # 5. List jobs (should be empty)
    print("5. tools/call transcript_jobs")
    r = await send_rpc("tools/call", {"name": "transcript_jobs", "arguments": {}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} jobs={data.get('data', {}).get('count', 0)}")
    print()

    # 6. Search (empty DB)
    print("6. tools/call transcript_search")
    r = await send_rpc("tools/call", {"name": "transcript_search", "arguments": {"query": "test"}})
    content = r.get("result", {}).get("content", [])
    if content:
        data = json.loads(content[0].get("text", "{}"))
        print(f"   success={data.get('success')} results={data.get('data', {}).get('count', 0)}")
    print()

    # 7. Read resource - stats
    print("7. resources/read transcript://stats")
    r = await send_rpc("resources/read", {"uri": "transcript://stats"})
    contents = r.get("result", {}).get("contents", [])
    if contents:
        data = json.loads(contents[0].get("text", "{}"))
        print(f"   total_transcripts={data.get('total_transcripts')} queue={data.get('queue')}")
    print()

    # 8. Ping
    print("8. ping")
    r = await send_rpc("ping")
    print(json.dumps(r, indent=2))
    print()

    print("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
