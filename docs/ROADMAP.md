# AI-media-RC Roadmap

> Living document tracking planned features, integrations, and infrastructure improvements.
> Items are grouped by domain and tagged with estimated complexity.

---

## 🟢 Recently Completed

- **Observability & Tracing** — Contextvars-based spans, Prometheus metrics, `/debug/traces` + `/metrics` endpoints
- **WebSocket Streaming** — `/ws/query` (full-duplex LLM streaming), `/ws/events` (push-based state updates)
- **Transcription DLQ & Crash Recovery** — Dead-letter queue, automatic stale-job recovery, health endpoints

---

## 🔵 Near-Term (Next Up)

### Infrastructure & Code Quality

| Item | Complexity | Notes |
|------|-----------|-------|
| Automated Test Suite & CI | Medium | Reorganize `scripts/` into pytest, add GitHub Actions, coverage gates |
| Agent Loop Resilience | Medium | Per-tool timeouts, circuit breaker, retry/backoff, fallback responses |
| Unified Configuration & DI | Medium | Pydantic Settings validation, service container, fail-fast startup |

### Frontend

| Item | Complexity | Notes |
|------|-----------|-------|
| Frontend Modernization | High | ES Modules → TypeScript → Lit Web Components → reactive WS state |

---

## 🟡 Mid-Term (Planned)

### Integrations

| Item | Complexity | Notes |
|------|-----------|-------|
| Arlo VMC5040 Push-to-Talk | **High** | See detailed assessment below |

### AI/ML

| Item | Complexity | Notes |
|------|-----------|-------|
| Speaker-aware voice sessions | Medium | Use diarization to track who's speaking and personalize responses |
| Semantic search improvements | Medium | Cross-modal search (transcript + EPG metadata + cast) |

---

## 🔴 Research / Long-Term

| Item | Complexity | Notes |
|------|-----------|-------|
| Multi-LLM routing | High | Route queries to specialized models (small for playback, large for search) |
| Android TV native remote | High | Dedicated Kotlin app with D-pad + voice, beyond the PWA |

---

## Detailed Assessments

### Arlo VMC5040 — Push-to-Talk (Two-Way Audio)

**How it works on the Arlo app:**

The VMC5040 uses **WebRTC** for two-way audio (not SIP — that's used on Arlo doorbells). The Arlo app sends a `StartStream` request to Arlo's cloud, receives an SDP offer, performs ICE negotiation, and opens a WebRTC data channel that carries microphone audio back to the camera speaker.

**Why pyaarlo can't do it:**

pyaarlo only implements the REST/MQTT cloud API — it has no WebRTC or ICE stack. Push-to-talk requires a full WebRTC session negotiation, which pyaarlo simply never implemented.

**Who has cracked it:**

The **Scrypted Arlo plugin** (`scryptedapp/arlo`) *does* implement two-way audio. It uses a compiled **Go binary** (`scrypted_arlo_go`) that handles WebRTC signaling and ICE negotiation as a child process. The Python plugin manages the Go process lifecycle and relays SDP between the Go binary and Arlo's cloud API.

**What AI-media-RC would need:**

1. A WebRTC client (Go, Node, or browser-based) that can accept an SDP offer from Arlo's cloud and send a mic audio track back
2. Or: embed/integrate the `scrypted_arlo_go` binary logic — it's open source
3. The bridge can expose a stub endpoint (`POST /cameras/<slug>/intercom/start`) for future wiring — AI-media-RC POSTs an SDP offer, the bridge relays it to Arlo cloud

**Complexity:** High. Not a pyaarlo limitation to fix — requires a separate WebRTC layer.

---

## Contributing

To propose a roadmap item, open an issue with the `roadmap` label or add directly to this file with a PR.
