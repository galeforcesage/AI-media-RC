#!/usr/bin/env python3
"""Show all request traces."""
import sys, json

with open("/tmp/orchestrator.log") as f:
    for line in f:
        idx = line.find("REQUEST_TRACE ")
        if idx < 0:
            continue
        t = json.loads(line[idx+14:])
        print(f'[{t["trace_id"]}] q="{t["query"][:40]}"  temporal={t["temporal"]}  domains={t["domains"]}  tools_offered={t["tools_offered"]}  steps={len(t["steps"])}  iters={t["iterations"]}  {t["total_ms"]:.0f}ms  ctx_est={t["context_tokens_est"]}  status={t["status"]}')
        for s in t.get("steps", []):
            err = f'  ERROR={s["error"]}' if s.get("error") else ""
            print(f'    {s["tool"]}({s["args_keys"]}) → {s["duration_ms"]:.0f}ms, {s["result_size"]}b{err}')
