#!/usr/bin/env python3
"""
remote_query.py — Server-side helper to query the orchestrator API.

Avoids all PowerShell/SSH/bash quoting issues by running directly on the server.

Usage:
    python3 /tmp/remote_query.py "what recorded yesterday"
    python3 /tmp/remote_query.py "what records tonight" --stream
    python3 /tmp/remote_query.py "what recorded yesterday" --trace
    python3 /tmp/remote_query.py --traces 5
    python3 /tmp/remote_query.py --log-grep "EntityContextStore|validation"
    python3 /tmp/remote_query.py --status
"""
import argparse
import json
import re
import sys
import urllib.request
import urllib.error

API = "http://127.0.0.1:8000/api"
LOG = "/tmp/orchestrator.log"


def query(prompt, stream=False):
    """Send a query to the orchestrator API."""
    endpoint = f"{API}/query/stream" if stream else f"{API}/query"
    payload = json.dumps({"prompt": prompt}).encode()
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if stream:
                for line in resp:
                    line = line.decode().strip()
                    if line.startswith("data: "):
                        evt = json.loads(line[6:])
                        if evt.get("type") == "status":
                            print(f"  [{evt['message']}]")
                        elif evt.get("type") == "token":
                            print(evt["token"], end="", flush=True)
                        elif evt.get("type") == "result":
                            print()
                            return evt.get("data", {})
            else:
                return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code}: {body[:500]}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


def show_traces(n=5):
    """Show the last N REQUEST_TRACE entries from the log."""
    traces = []
    with open(LOG) as f:
        for line in f:
            idx = line.find("REQUEST_TRACE ")
            if idx < 0:
                continue
            try:
                traces.append(json.loads(line[idx + 14:]))
            except Exception:
                pass
    for t in traces[-n:]:
        steps = t.get("steps", [])
        step_summary = ", ".join(
            f"{s['tool']}({s.get('duration_ms',0):.0f}ms)"
            for s in steps
        )
        print(
            f"  [{t['trace_id']}] {str(t.get('status','?')):15s} "
            f"val={str(t.get('validation','n/a')):6s} "
            f"ent={str(t.get('entity_count','?')):>3s} "
            f"tools={str(t.get('tools_offered','?')):>2s} "
            f"iters={t.get('iterations','?')} "
            f"{t.get('total_ms',0):.0f}ms "
            f"q={t['query'][:50]}"
        )
        if steps:
            print(f"           steps: {step_summary}")


def log_grep(pattern, n=20):
    """Grep the orchestrator log for a pattern."""
    regex = re.compile(pattern, re.I)
    matches = []
    with open(LOG) as f:
        for line in f:
            if regex.search(line):
                matches.append(line.rstrip())
    for m in matches[-n:]:
        print(m)


def status():
    """Check API health."""
    try:
        with urllib.request.urlopen(f"{API}/../health", timeout=5) as r:
            print(json.dumps(json.loads(r.read()), indent=2))
    except Exception:
        # Try the root
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/", timeout=5) as r:
                print(f"HTTP 200 — orchestrator is up")
        except Exception as e:
            print(f"Orchestrator unreachable: {e}")


def main():
    parser = argparse.ArgumentParser(description="Query the orchestrator API")
    parser.add_argument("prompt", nargs="?", help="Query prompt")
    parser.add_argument("--stream", action="store_true", help="Use SSE streaming endpoint")
    parser.add_argument("--trace", action="store_true", help="Show trace after query")
    parser.add_argument("--traces", type=int, metavar="N", help="Show last N traces from log")
    parser.add_argument("--log-grep", metavar="PATTERN", help="Grep orchestrator log")
    parser.add_argument("--status", action="store_true", help="Check API health")
    args = parser.parse_args()

    if args.status:
        status()
        return

    if args.traces:
        show_traces(args.traces)
        return

    if args.log_grep:
        log_grep(args.log_grep)
        return

    if not args.prompt:
        parser.print_help()
        sys.exit(1)

    result = query(args.prompt, stream=args.stream)
    if result:
        resp = result.get("llm_response", result.get("response", ""))
        st = result.get("status", "?")
        iters = result.get("iterations", "?")
        print(f"\nStatus: {st}  Iterations: {iters}")
        print(f"Response:\n{resp[:500]}")

    if args.trace:
        print("\n--- Last trace ---")
        show_traces(1)


if __name__ == "__main__":
    main()
