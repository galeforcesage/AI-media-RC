# Deterministic dynamic surfaces

AI-media-RC supports an optional local dynamic-surface layer for conversational
results. It uses the A2UI v0.9.1 protocol identifier and a deliberately narrow
LLM Remote catalog, while keeping authoritative domain data and layout decisions
in Python.

## Design constraints

- The local LLM does not generate arbitrary component JSON, HTML, endpoints, or
  tool calls.
- Tool results remain authoritative and always include a text fallback.
- The server compiles and validates each surface against
  `frontend/catalogs/llm-remote-v1.json`.
- The browser renderer is bundled locally in `frontend/js/a2ui.js`; no CDN or
  cloud model is required.
- Privileged operations are stored server-side and represented in the browser by
  a single-use opaque action ID bound to the app session.

## Initial catalog

- `RecordingList` — read-only cards compiled from DVR listing results.
- `ConfirmationCard` — confirm/cancel controls for destructive and owner tools.
- `ErrorNotice` — reserved structured error presentation.

## Transport

`POST /api/query/stream` emits an SSE event:

```text
data: {"type":"ui","message":{...validated surface...}}
```

The final result retains `ui_messages` so non-streaming clients can render the
same surface. Existing response text remains available as the accessibility and
compatibility fallback.

## Trusted actions

When an agent tool requires confirmation:

1. The backend removes the raw tool name and arguments from the client response.
2. `PendingActionStore` retains the operation for a short configured lifetime.
3. The client receives only `pending_action_id` and Confirm/Cancel controls.
4. `POST /api/ui/action` accepts the opaque ID and requested disposition.
5. The backend checks ownership, expiration, single-use state, and the registered
   confirmation allowlist before execution.

## Configuration

```json
{
  "ui": {
    "enabled": true,
    "protocol": "a2ui-v0.9.1",
    "mode": "deterministic",
    "renderer": "local-web-components",
    "catalog_id": "urn:llm-remote:a2ui:catalog:v1",
    "allow_model_layout": false,
    "max_components_per_surface": 100,
    "pending_action_ttl_seconds": 300,
    "text_fallback": true
  }
}
```

The persistent remote controls, authentication, settings, transcript editor,
voice capture, and admin controls remain conventional UI.
