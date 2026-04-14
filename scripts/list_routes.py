#!/usr/bin/env python3
"""List all registered FastAPI routes."""
import json, urllib.request, sys
resp = urllib.request.urlopen("http://127.0.0.1:8000/openapi.json")
data = json.loads(resp.read().decode())
for path in sorted(data.get("paths", {}).keys()):
    methods = list(data["paths"][path].keys())
    print(f"  {', '.join(m.upper() for m in methods):8s} {path}")
