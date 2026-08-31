# Shared-GPU arbitration (VSR / LLM / Whisper / Paperless)

The RTX 5080 (16 GB) is shared by four tenants. They must not fight over VRAM,
and live TV must never stutter. This document describes the arbiter that
coordinates them and how to wire the external Paperless stack into it.

## Priority order (lower number wins)

| # | Tenant | Nature | How it's handled |
|---|--------|--------|------------------|
| 0 | **VSR live-TV upscale** (SageTV plugin, NVENC) | Real-time, non-cooperative | *Observed*, never gated. Detected via NVENC encoder utilisation + free VRAM. We always leave it a ~6.5 GB reserve. |
| 1 | **AI-remote interactive LLM** | User waiting | Wrapped by the arbiter: pauses Whisper, picks the biggest model that fits, unloads quickly (short `keep_alive`). |
| 2 | **AI-remote batch Whisper / diarization** | Delay-tolerant | Yields the GPU on request — checkpoints at the next segment, unloads, resumes where it left off. |
| 3 | **Paperless easyOCR + Paperless summariser LLM** | Batch, lowest | Asks the arbiter for a lease over HTTP and backs off while VSR is live *or* an AI-remote session is active. |

## What the arbiter does per LLM turn

`backend/orchestrator/src/services/gpu_arbiter.py` (`GpuArbiter`), driven from
`LLMService._begin_turn` / `_end_turn`:

1. Mark an interactive session active (so Paperless queues).
2. **Pause batch Whisper** via a JSON-RPC `gpu/pause` to the transcription
   service (`:8770`). The running job checkpoints at its next segment boundary
   and unloads, freeing its VRAM within ~1 s.
3. **Select the model** from the free VRAM the card actually has:
   * VSR **idle** → biggest tier that fits (`qwen3:14b`, ~12.5 GB @ 32k ctx) —
     the most accurate model, using almost the whole card.
   * VSR **live** (NVENC busy) → floor tier (`hermes3:8b`, ~6.6 GB @ 16k) so we
     never crowd live TV.
4. Generate with a short `keep_alive` so the model releases quickly and VSR can
   reclaim the card within seconds.
5. **Resume Whisper** (`gpu/resume`); the paused job continues from its
   checkpoint offset.

If VSR starts *mid-generation* while the big model is loaded, the next turn
measures the reduced free VRAM (and the encoder signal) and automatically drops
back to the floor tier — the "shift back to A" behaviour.

Everything fails safe: if nvidia-smi or the transcription service is
unreachable, the LLM path falls back to its configured model and never blocks.

Tunables (env): `GPU_VSR_RESERVE_MB` (6500), `GPU_SAFETY_MB` (700),
`GPU_ENCODER_LIVE_PCT` (3), `GPU_INTERACTIVE_TTL` (20), `GPU_LLM_KEEP_ALIVE`
(30s), `GPU_FLOOR_MODEL` (hermes3:8b).

## Persistent config (survives restarts)

The model ladder and floor are read from a `gpu_arbiter` block in
`backend/orchestrator/config.json`; if the block is absent the arbiter uses the
`DEFAULT_LADDER` baked into `gpu_arbiter.py`, so intent survives even a scrubbed
config:

```json
"gpu_arbiter": {
    "floor_model": "hermes3:8b",
    "model_ladder": [
        {"model": "qwen3:14b", "vram_mb": 12800, "num_ctx": 32768},
        {"model": "mistral-nemo:latest", "vram_mb": 9400, "num_ctx": 16384},
        {"model": "hermes3:8b", "vram_mb": 6800, "num_ctx": 16384}
    ]
}
```

`scripts/watchdog.sh` regenerates the `llm` and `gpu_arbiter` blocks on every
orchestrator start (`ensure_orchestrator_config`), so a restart re-asserts this
intent rather than clobbering it, and `ensure_ollama_models` always pre-pulls
the floor model plus the top model so the arbiter's picks are guaranteed
present. Both use `setdefault` for `gpu_arbiter`, so hand-tuned overrides in
`config.json` are preserved. `qwen2.5:32b` is deliberately excluded from the
ladder: it spills to CPU (~7 tok/s) on a 16 GB card.


## HTTP lease API (for lower-priority tenants)

Served by the orchestrator on `:8000` under `/api`:

* `GET  /api/gpu/state` → `{interactive_active, vsr_live, free_mb, encoder_util}`
* `POST /api/gpu/lease` body `{tenant, priority, vram_mb}` →
  `{granted: bool, reason?, retry_after_s?, free_mb?, vsr_live?}`

A lease is **denied** when an interactive AI-remote session is active, or when
the request plus the VSR reserve wouldn't fit in free VRAM.

## Wiring Paperless-NGX into the arbiter

Paperless config lives outside this repo, so two drop-in helpers are provided in
`scripts/`.

### 1. easyOCR — pre-consume guard

`scripts/paperless-gpu-guard.sh` blocks until a lease is granted, then exits 0
(fail-open after `GPU_GUARD_MAX_WAIT`, default 30 min). Bind-mount it into the
Paperless webserver container and set it as the pre-consume script:

```yaml
# docker-compose override for the paperless "webserver" service
services:
  webserver:
    volumes:
      - /media/sagetv/nextcloud/AI-media-RC/scripts/paperless-gpu-guard.sh:/usr/local/bin/paperless-gpu-guard.sh:ro
    environment:
      PAPERLESS_PRE_CONSUME_SCRIPT: /usr/local/bin/paperless-gpu-guard.sh
      ARBITER_URL: http://10.0.0.10:8000/api   # host IP reachable from the container
      GPU_GUARD_VRAM: "4000"
```

OCR for a newly consumed document then only starts once the GPU is free of
higher-priority work.

### 2. Paperless-AI summariser — priority Ollama proxy

Paperless-AI calls the *same* shared Ollama. Put `scripts/ollama-priority-proxy.py`
in front of it: it acquires a low-priority lease before forwarding
`/api/generate` and `/api/chat`, holding the request while VSR is live or an
AI-remote session is active. All other Ollama endpoints pass straight through.
Stdlib-only; no pip installs.

```bash
# run on the host (systemd unit or nohup); host-network so it can see :11434 and :8000
OLLAMA_URL=http://127.0.0.1:11434 \
ARBITER_URL=http://127.0.0.1:8000/api \
LISTEN_PORT=11500 \
python3 /home/sagetv/AI-media-RC/scripts/ollama-priority-proxy.py
```

Then point Paperless-AI's Ollama base URL at `http://10.0.0.10:11500`
instead of `:11434`. Its LLM calls now queue behind live TV and the AI-remote.
