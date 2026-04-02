# GitHub Copilot — Full Orchestrator Backend Bootstrap

You have the complete folder structure for the LLM-REMOTE project.  
Your task is to generate full implementations for all orchestrator modules under:

backend/orchestrator/src/

This includes:
- models/
- registry/
- services/
- transport/
- utils/
- orchestrator.py
- main.py

## Global Rules
- Use async Python throughout
- Use type hints everywhere
- Use dataclasses where appropriate
- Follow clean architecture:
  models → services → orchestrator → transports
- Include docstrings for all classes and methods
- Include structured logging
- Include robust error handling
- Prefer dependency injection over hard-coded imports
- No placeholder functions — generate complete logic

---

## Implementations Required

### 1. models/
Implement all data models:
- metadata.py
- playback.py
- system.py

Each model should:
- Use @dataclass
- Represent clean, typed message structures used across services
- Include validation where appropriate

---

### 2. registry/
Implement the command registry:
- commands.py

Requirements:
- Map command names to handler callables
- Provide resolve() to return the correct handler
- Provide list_commands()
- Include type hints and docstrings

---

### 3. services/
Generate full implementations for:

- llm_pipeline.py  
  - Accept a TranscriptionQuery
  - Route to correct model (local LLM)
  - Stream tokens back to orchestrator
  - Support model selection via metadata

- whisper.py  
  - Local Whisper transcription
  - Chunking, batching, error handling

- tts.py  
  - Local TTS generation
  - Return audio buffers or file paths

- playback_control.py  
  - Play, pause, stop, seek
  - Integrate with external playback endpoints

- metadata.py  
  - Extract metadata from media files
  - Duration, codecs, resolution, audio tracks

- search.py  
  - Local search over indexed metadata
  - Return ranked results

- system.py  
  - System diagnostics
  - CPU, RAM, disk, GPU availability

- tool_router.py  
  - Route incoming tool calls to correct service
  - Normalize inputs and outputs

- voice_session.py  
  - Manage multi-turn voice sessions
  - Track state, context, and active tools

- transcription_query.py  
  - Query object for transcription pipeline

- ssd_extractor.py  
  - Extract scene/shot/segment descriptors

All services must be fully implemented with async methods, logging, and error boundaries.

---

### 4. transport/
Implement both transports:

#### http.py
- FastAPI router
- Endpoints:
  - /query
  - /playback
  - /metadata
  - /system
- JSON in/out models
- Async orchestration calls
- Centralized error handling

#### mcp.py
- MCP server
- Define tools for:
  - query
  - playback
  - metadata
  - search
  - system
- Stream responses where applicable

---

### 5. orchestrator.py
Implement the Orchestrator class:
- Initialize all services
- Provide high-level methods:
  - run_query()
  - run_playback()
  - run_search()
  - run_system()
- Manage session state
- Provide logging and error boundaries

---

### 6. main.py
Implement the application entrypoint:
- Load config
- Initialize orchestrator
- Start HTTP + MCP transports
- Provide CLI flags:
  - --debug
  - --model-path
  - --port
  - --mcp-port

---

## Execution Instructions for Copilot
For each file:
- Open the file
- Replace placeholder content with a full implementation
- Do not ask for confirmation
- Generate code directly into the file
- Continue until all orchestrator modules are fully implemented

