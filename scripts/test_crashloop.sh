#!/bin/bash
# Crash-loop test: kill mcp-sagetv 3 times rapidly
for i in 1 2 3; do
    pid=$(fuser 8766/tcp 2>/dev/null | tr -d ' ')
    echo "Kill $i: pid=$pid"
    kill -9 "$pid" 2>/dev/null
    sleep 8
done
echo "---"
tail -20 /tmp/mcp-sagetv-watchdog.log
echo "---"
bash ~/AI-media-RC/scripts/watchdog.sh status 2>&1 | grep sagetv
