# AI-media-RC Roadmap

> Living document tracking planned features, integrations, and infrastructure improvements.
>
> Last audited: 2026-10-19 against the current branch, `origin/main`, available
> remote branch history, source, documentation, and tests.

## Status definitions

- **implemented** — the repository contains the intended end-to-end behavior.
- **partially implemented** — meaningful foundations exist, but the stated outcome is incomplete.
- **not implemented** — no functional implementation of the roadmap outcome was found.
- **superseded** — another design replaced the roadmap item.
- **unable to determine** — repository evidence is insufficient.

## Audit summary

| Item | Classification | Concrete repository evidence |
|------|----------------|------------------------------|
| Observability and tracing | **implemented** | `backend/orchestrator/src/utils/tracing.py` provides trace/span context and a completed-trace buffer. `utils/metrics.py` defines Prometheus counters, gauges, and histograms. `api/routes/debug.py` exposes `/debug/traces`, `/debug/traces/{trace_id}`, and `/metrics`. HTTP, LLM, MCP, and agent code import the instrumentation. |
| WebSocket streaming and push state | **partially implemented** | `transport/ws.py` exposes `/ws/query` and `/ws/events`; `frontend/js/api.js` defines `queryWS()` and `connectEvents()`. However, `frontend/js/app.js` still sends user queries through SSE `API.queryStream()`, `queryWS()` has no caller, `broadcast_event()` has no caller, and `state.js` still starts the 7-second and 30-second polling timers even after opening `/ws/events`. |
| Deterministic A2UI dynamic surfaces | **partially implemented** | The orchestrator now compiles trusted recording lists and opaque confirmation cards, validates them against a narrow local catalog, emits SSE `ui` events, and renders them with a same-origin local component renderer. Remaining catalog surfaces include transcripts, EPG/program cards, diagnostics, clarification choices, and progress updates. |
| Transcription DLQ and crash recovery | **implemented** | `backend/transcription/src/queue.py` migrates `dead_letter_reason`, recovers stale jobs, lists/retries dead-letter jobs, and reports the count. `worker.py` runs recovery at startup and every five minutes. `server.py` exposes health, dead-letter listing, and retry MCP tools. |
| Automated test suite and CI | **partially implemented** | `backend/orchestrator/pyproject.toml` includes pytest dependencies and `backend/orchestrator/tests/` contains three pytest files. About 31 additional `test_*.py` files remain under `scripts/`; no `.github/workflows/` files, pytest configuration, coverage configuration, or coverage gate were found. |
| Agent loop resilience | **partially implemented** | MCP reads have a 30-second timeout in `services/mcp_client.py`; failed connections can invoke the watchdog with a 120-second restart cooldown; the agent retries empty LLM output and normalized search variants. There is no per-tool configurable timeout policy, failure-class-aware exponential backoff, namespace circuit breaker, or circuit state surfaced to the planner. |
| Unified configuration and dependency injection | **partially implemented** | `utils/config.py` loads JSON/YAML, applies environment overrides, and validates selected OpenClaw fields. `orchestrator.py` still accepts `Dict[str, Any]`, performs extensive nested `.get()` wiring, and directly constructs services. No Pydantic Settings schema, generated config schema, or service container exists. |
| Frontend modernization | **partially implemented** | The frontend has a WebSocket API seam and is split into several JavaScript files, but it remains global IIFE-style JavaScript (`API`, `State`, `UI`, `Voice`). No TypeScript, ES-module imports, Lit components, package manifest, build pipeline, or frontend unit-test framework was found. |
| Arlo VMC5040 push-to-talk | **not implemented** | Repository search found Arlo, WebRTC, SDP, ICE, `pyaarlo`, `scrypted_arlo_go`, and `/intercom/start` only in this roadmap. No Arlo bridge or WebRTC client exists in the source. |
| Speaker-aware voice sessions | **partially implemented** | The transcription subsystem performs pyannote diarization, aligns speakers to transcript segments, and can map speaker labels to cast/character names (`transcription/src/diarization.py`, `worker.py`, and `enrichment.py`). `orchestrator/src/services/voice_session.py` does not identify the live speaker, retain a speaker/user profile, or personalize responses by speaker. |
| Semantic search improvements | **partially implemented** | `services/semantic_index.py` embeds SageTV and Channels recording metadata in ChromaDB and the orchestrator prefetches semantic context. The transcription service separately implements FTS5 cross-metadata filters for actor, genre, channel, dates, and system. The vector index does not embed transcript chunks, cast, or EPG results, so the proposed unified transcript + EPG + cast semantic retrieval is incomplete. |
| Multi-LLM routing | **not implemented** | The planner registry can choose AgentLoop or OpenClaw, including a shadow planner, but `LLMService` is configured with one Ollama endpoint/model. No query classifier routes workloads among multiple LLM models. |
| Android TV native remote | **not implemented** | `android/channels-bridge` is a native Kotlin foreground bridge/configuration app and declares Leanback support, but it proxies Channels API commands and is not an end-user D-pad/voice remote. The user-facing remote remains the PWA. |
| Server-side transcript edit persistence | **not implemented** | `frontend/js/app.js` explicitly says `TODO: save to backend when endpoint is available` and only updates the DOM. The orchestrator exposes transcript GET/search routes but no update route, and the transcription store has internal ingestion `save()` rather than an authenticated edit API. |
| Pluggable GPU lease/router seam | **partially implemented** | This remembered item is absent from the current branch and `origin/main`. Remote branch `origin/galeforcesage-redesigned-guacamole` contains `services/lease.py`, `LeaseManager`, `NullLeaseManager`, `GPU_ROUTER_PLUGIN`, and LLM/orchestrator wiring (notably commit `7a1c097`). It remains outstanding until that public, fail-open seam is integrated into the target branch. |

## Completed

### Observability and tracing

The core tracing and metrics surfaces are present. Follow-up work belongs under
testing and operational hardening rather than the original implementation item.

### Transcription DLQ and crash recovery

Persistent dead-letter handling, startup recovery, periodic recovery, health
reporting, listing, and manual retry are present.

## Near-term

### Automated test suite and CI

1. Move or adapt meaningful `scripts/test_*.py` checks into package-local pytest suites.
2. Add shared fixtures for Ollama, MCP servers, queue databases, and configuration.
3. Configure pytest and coverage in `pyproject.toml`.
4. Add GitHub Actions for supported Python versions and enforce a coverage floor.
5. Separate hardware/live-service integration tests from deterministic unit tests.

### Agent loop resilience

1. Add configurable per-tool and per-namespace deadlines.
2. Classify transient transport failures separately from invalid arguments and backend errors.
3. Retry only transient failures with bounded exponential backoff and jitter.
4. Add per-namespace circuit breakers with observable open/half-open state.
5. Tell the planner which services are degraded so it can avoid impossible calls.

### Unified configuration and dependency injection

1. Replace raw dictionaries with nested validated settings models.
2. Define predictable, typed environment-variable overrides.
3. Fail startup with clear messages for invalid required values.
4. Move service construction from `Orchestrator.__init__` into a testable service container.
5. Generate configuration JSON Schema and deployment documentation.

### WebSocket completion

1. Publish playback, health, device, and transcription events through `broadcast_event()`.
2. Migrate the main query UI from SSE `queryStream()` to `queryWS()`, or remove the unused path.
3. Disable polling while the event socket is healthy and restore it only after disconnect.
4. Implement subscription filtering or remove the documented but unsupported subscribe action.
5. Add reconnect/backpressure tests and authenticate WebSocket handshakes consistently.

### Dynamic-surface catalog expansion

1. Add transcript-hit, program/EPG, clarification, metric, service-status, job,
   structured-error, and progress components.
2. Add trusted action mappings for non-privileged playback, transcript, and
   details actions without exposing arbitrary endpoint or tool payloads.
3. Normalize transcript and metadata card construction in the backend.
4. Evaluate the official locally bundled Lit renderer after the deterministic
   catalog and action contract stabilize.
5. Keep unrestricted model-authored layout disabled unless a separate
   validation and evaluation gate proves it reliable on local models.

### Frontend modernization

1. Convert global IIFEs to ES modules while preserving current behavior.
2. Add TypeScript and a minimal build pipeline.
3. Incrementally componentize the device picker, now-playing view, D-pad, search, and transcript UI.
4. Replace timer-driven state with a reactive store fed by the completed event socket.
5. Add frontend unit and integration tests while retaining PWA/offline behavior.

### Server-side transcript edit persistence

1. Add an authenticated transcript update endpoint with validation and conflict handling.
2. Update transcript metadata, sidecar data, FTS records, and semantic indexes atomically.
3. Preserve an audit/revision trail so corrections are recoverable.
4. Wire the existing transcript Save button to the endpoint and surface failures.

### Pluggable GPU lease/router seam

1. Reconcile the fail-open lease seam from `origin/galeforcesage-redesigned-guacamole`.
2. Keep the public repository broker-agnostic with `NullLeaseManager` as the default.
3. Load deployment-specific routing through `GPU_ROUTER_PLUGIN`.
4. Add tests for no-plugin, broken-plugin, lease-success, and fallback behavior.

## Mid-term

### Arlo VMC5040 push-to-talk (two-way audio)

**How it works in the Arlo app**

The VMC5040 uses **WebRTC** for two-way audio, rather than the SIP path used by
some Arlo doorbells. The Arlo app sends a `StartStream` request to Arlo's cloud,
receives an SDP offer, performs ICE negotiation, and opens a WebRTC channel that
carries microphone audio back to the camera speaker.

**Why pyaarlo cannot provide it**

`pyaarlo` implements the REST/MQTT cloud API but does not include a WebRTC or ICE
stack. Push-to-talk therefore requires a separate real-time media layer rather
than a normal pyaarlo API addition.

**Existing reference implementation**

The Scrypted Arlo plugin (`scryptedapp/arlo`) implements two-way audio using a
compiled Go helper (`scrypted_arlo_go`) for WebRTC signaling and ICE. Its Python
plugin manages the helper process and relays SDP to and from Arlo's cloud API.

**Required work**

1. Implement a WebRTC client in Go, Node, or a suitable browser runtime that can
   accept Arlo's SDP offer and send a microphone audio track.
2. Alternatively, evaluate integrating or adapting the open-source
   `scrypted_arlo_go` logic, subject to license and maintenance review.
3. Define a bridge contract such as
   `POST /cameras/<slug>/intercom/start` for session creation and SDP exchange.
4. Add session teardown, ICE failure handling, authentication, and microphone
   permission UX.

**Complexity:** High. This is not a pyaarlo defect; it requires a separate
WebRTC signaling and media implementation.

### Speaker-aware voice sessions

Build on the existing offline diarization pipeline to identify a live speaker or
associate a voice session with an explicit household profile. Keep uncertain
identity opt-in and avoid treating recording character labels as user identity.

### Unified semantic retrieval

Extend the existing metadata vector index and transcript FTS into one retrieval
layer that can rank transcript chunks, EPG entries, recording metadata, cast,
genres, and dates with source-aware filters and explainable citations.

## Research / long-term

### Multi-LLM routing

Route low-risk playback and status requests to a small fast model and reserve a
larger model for transcript synthesis and complex planning. This should build on,
but remain distinct from, the existing planner registry and optional GPU lease
mechanism.

### Android TV native remote

Create a dedicated Kotlin remote with D-pad, voice input, search, playback state,
and device/session selection. Reuse bridge/networking components where useful,
but keep the bridge service and user-facing remote responsibilities separate.

## Contributing

When updating an item, include:

1. Its classification.
2. Concrete paths, tests, routes, or commits supporting the classification.
3. Remaining acceptance criteria for anything not fully implemented.
