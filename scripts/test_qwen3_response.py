#!/usr/bin/env python3
"""Test qwen3:8b response format — check for thinking tags in final answers."""
import urllib.request, json

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:8b"

def api_call(endpoint, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{OLLAMA}{endpoint}", data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())

# Simulate what happens after tool results come back
messages = [
    {"role": "system", "content": "You are a home media assistant. Today is 2026-04-25. List recordings in format: \"ShowName\" \"EpisodeTitle\" S##E## ✓"},
    {"role": "user", "content": "What recorded last Thursday?"},
    {"role": "assistant", "content": "", "tool_calls": [{"id":"call_1","function":{"name":"channels_search_recordings","arguments":{"start_date":"2026-04-23","end_date":"2026-04-23"}}}]},
    {"role": "tool", "content": json.dumps({"success": True, "message": "3 shows were recorded, all on the DVR", "data": {"results": [
        {"title": "Matlock", "episode_title": "Day One", "season_episode": "S02E14", "watched": True, "status": "available"},
        {"title": "Ghosts", "episode_title": "Woodstone Royale", "season_episode": "S05E16", "watched": True, "status": "available"},
        {"title": "Next Level Chef", "episode_title": "The Tournament", "season_episode": "S05E11", "watched": False, "status": "available"},
    ]}})},
]

r = api_call("/api/chat", {"model": MODEL, "messages": messages, "stream": False})
msg = r.get("message", {})
content = msg.get("content", "")

print("=== Full response ===")
print(content)
print(f"\n=== Stats ===")
print(f"Length: {len(content)} chars")
print(f"Has <think> tags: {'<think>' in content}")

if "<think>" in content:
    # Show what it looks like after stripping
    import re
    cleaned = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
    print(f"\n=== After stripping <think> ===")
    print(cleaned)
