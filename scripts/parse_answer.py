#!/usr/bin/env python3
import sys, json
r = json.load(sys.stdin)
print(r.get("answer", "NO ANSWER")[:2000])
