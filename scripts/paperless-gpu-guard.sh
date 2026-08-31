#!/usr/bin/env bash
# paperless-gpu-guard.sh
#
# Block until the AI-media-remote GPU arbiter says a low-priority tenant may use
# the shared RTX 5080, then exit 0. Paperless-NGX (easyOCR) and any other batch
# GPU job should call this *before* touching the card, so they queue while:
#   * an AI-remote interactive LLM session is active, or
#   * VSR live-TV upscaling is running, or
#   * there simply isn't enough free VRAM.
#
# It never blocks forever: after GPU_GUARD_MAX_WAIT seconds it gives up and
# exits 0 (fail-open) so document ingestion can't wedge permanently. It also
# fails open if the arbiter is unreachable.
#
# Wire-up (Paperless-NGX): set this as the pre-consume script, e.g. in the
# webserver container's environment:
#   PAPERLESS_PRE_CONSUME_SCRIPT=/usr/local/bin/paperless-gpu-guard.sh
# and bind-mount this file to that path. easyOCR OCR then only starts once
# granted.
#
# Env:
#   ARBITER_URL      base URL of the orchestrator API (default host:8000/api)
#   GPU_GUARD_VRAM   MiB the caller expects to use (default 4000)
#   GPU_GUARD_PRIO   priority, 3 = paperless/lowest (default 3)
#   GPU_GUARD_POLL   seconds between polls (default 10)
#   GPU_GUARD_MAX_WAIT  give-up ceiling in seconds (default 1800)

set -u

ARBITER_URL="${ARBITER_URL:-http://10.0.0.10:8000/api}"
VRAM="${GPU_GUARD_VRAM:-4000}"
PRIO="${GPU_GUARD_PRIO:-3}"
POLL="${GPU_GUARD_POLL:-10}"
MAX_WAIT="${GPU_GUARD_MAX_WAIT:-1800}"

start=$(date +%s)
while true; do
  resp=$(curl -s -m 5 -X POST "${ARBITER_URL}/gpu/lease" \
    -H 'Content-Type: application/json' \
    -d "{\"tenant\":\"paperless\",\"priority\":${PRIO},\"vram_mb\":${VRAM}}" 2>/dev/null)

  # Arbiter unreachable -> fail open rather than stall ingestion.
  if [ -z "$resp" ]; then
    echo "gpu-guard: arbiter unreachable, proceeding (fail-open)" >&2
    exit 0
  fi

  if echo "$resp" | grep -q '"granted"[[:space:]]*:[[:space:]]*true'; then
    exit 0
  fi

  now=$(date +%s)
  if [ $((now - start)) -ge "$MAX_WAIT" ]; then
    echo "gpu-guard: waited ${MAX_WAIT}s, proceeding anyway" >&2
    exit 0
  fi

  echo "gpu-guard: GPU busy ($(echo "$resp" | tr -d '\n')), waiting ${POLL}s" >&2
  sleep "$POLL"
done
