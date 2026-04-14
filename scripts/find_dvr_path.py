#!/usr/bin/env python3
"""Find DVR base path for recordings."""
import json, os, subprocess, urllib.request

# Get full details for recording 12260
rec = json.loads(urllib.request.urlopen("http://localhost:8089/dvr/files/12260").read())
path = rec.get("Path", "")
print(f"Path: {path}")

# Check all keys except Airing
for k in sorted(rec.keys()):
    if k != "Airing" and ("path" in k.lower() or "dir" in k.lower() or "file" in k.lower() or "loc" in k.lower()):
        print(f"  {k}: {rec[k]}")

# Find where Channels DVR stores files
result = subprocess.run(["find", "/", "-maxdepth", "5", "-name", "*.mpg", "-path", "*/TV/The Neighborhood/*", "-type", "f"],
                       capture_output=True, text=True, timeout=30)
print(f"\nFilesystem search:")
for line in (result.stdout.strip().split("\n") if result.stdout.strip() else []):
    print(f"  {line}")

# Check DVR data dir
result2 = subprocess.run(["find", "/", "-maxdepth", "3", "-name", "data", "-type", "d"],
                        capture_output=True, text=True, timeout=15)
print(f"\nData dirs:")
for line in (result2.stdout.strip().split("\n") if result2.stdout.strip() else ["none"]):
    if "channels" in line.lower() or "dvr" in line.lower():
        print(f"  {line}")

# Check channels-dvr process for hints
result3 = subprocess.run(["ps", "aux"], capture_output=True, text=True)
for line in result3.stdout.split("\n"):
    if "channels" in line.lower() and "dvr" in line.lower():
        print(f"\nChannels process: {line.strip()}")
