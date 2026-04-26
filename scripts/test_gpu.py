#!/usr/bin/env python3
"""Quick test: Ollama inference speed + GPU detection."""
import urllib.request, json, time

start = time.time()
data = json.dumps({"model": "qwen2.5:latest", "prompt": "Say hello", "stream": False}).encode()
req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=data, headers={"Content-Type": "application/json"})
resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
elapsed = time.time() - start

print(f"Response: {resp.get('response', '')[:80]}")
print(f"Total time: {elapsed:.1f}s")
print(f"Eval tokens: {resp.get('eval_count')}")
eval_ns = resp.get("eval_duration", 0)
prompt_ns = resp.get("prompt_eval_duration", 0)
print(f"Prompt eval: {prompt_ns/1e9:.1f}s")
print(f"Token eval: {eval_ns/1e9:.1f}s")
if eval_ns and resp.get("eval_count"):
    tps = resp["eval_count"] / (eval_ns / 1e9)
    print(f"Speed: {tps:.1f} tok/s")
    if tps > 30:
        print("GPU acceleration: LIKELY (>30 tok/s)")
    elif tps > 15:
        print("GPU acceleration: POSSIBLE (15-30 tok/s)")
    else:
        print("GPU acceleration: UNLIKELY (<15 tok/s, CPU-like speed)")

# Also check ollama ps for processor info
import subprocess
result = subprocess.run(["ollama", "ps"], capture_output=True, text=True)
print(f"\n{result.stdout}")
