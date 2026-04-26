#!/usr/bin/env python3
"""Directly call channels_search_recordings MCP tool for April 16 and inspect results."""
import json, socket

def mcp_call(method, params=None, port=8767):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    raw = json.dumps(msg).encode()
    header = f"Content-Length: {len(raw)}\r\n\r\n".encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(("127.0.0.1", port))
    s.sendall(header + raw)
    buf = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
        if b"\r\n\r\n" in buf:
            header_part, body_part = buf.split(b"\r\n\r\n", 1)
            for line in header_part.decode().split("\r\n"):
                if line.lower().startswith("content-length:"):
                    clen = int(line.split(":")[1].strip())
            while len(body_part) < clen:
                body_part += s.recv(4096)
            s.close()
            return json.loads(body_part[:clen])
    s.close()
    return None

result = mcp_call("tools/call", {
    "name": "channels_search_recordings",
    "arguments": {"start_date": "2026-04-16", "end_date": "2026-04-16"}
})

if result and "result" in result:
    content = result["result"].get("content", [])
    for c in content:
        if c.get("type") == "text":
            data = json.loads(c["text"])
            print(f"Message: {data.get('message')}")
            print(f"Total results: {len(data.get('data', {}).get('results', []))}")
            for rec in data["data"]["results"]:
                status = rec.get("status", "?")
                title = rec.get("title", "?")
                ep = rec.get("episode_title", "")
                se = rec.get("season_episode", "")
                watched = rec.get("watched", False)
                print(f"  [{status:>9}] {title} - {ep} {se} {'(watched)' if watched else ''}")
else:
    print("Error:", json.dumps(result, indent=2)[:500])
