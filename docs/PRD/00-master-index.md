=========================================
MASTER INDEX — Unified LLM Remote Control System
=========================================
This index ties together the entire PRD and all appendices.
0. Executive Summary
Overview of the unified HTML5 LLM Remote Control System for SageTV + ChannelsDVR.
1. Project Objectives
Goals, scope, and constraints.
2. System Architecture
Highlevel architecture, data flow, and component interactions.
3. HTML5 Remote
UI/UX, device picker, system picker, voice input, LLM responses.
4. Unified Session Manager
Device binding, session resolution, playback context.
5. STT/TTS Subsystem
Model selection, browser STT, server STT, TTS.
6. LLM Gateway
Prompting, context injection, tool routing.
7. MCP Architecture
Three MCP servers + orchestrator.
8. Security Model
Authentication, authorization, Responsible AI.
9. Deployment Model
Docker, local LLM, local STT/TTS.

Appendices
Appendix A — SageTV Capability Dictionary
Entities, actions, resources, tools, safety levels.
Appendix B — ChannelsDVR Capability Dictionary
Entities, actions, resources, tools, safety levels.
Appendix C — MCP Architecture
Three servers + orchestrator.
Appendix D — Transcription Subsystem
Watchers, queue, Whisper, metadata, MCP exposure.
Appendix E — Device Registry & Pairing Protocol
Device metadata, pairing, admin panel.
Appendix F — HTML5 Remote UI Specification
Layout, controls, states, admin panel.
Appendix G — MCP Tool Definitions
Full tool registry for SageTV, ChannelsDVR, Linux.
Appendix H — LLM Prompt Specification
System prompt, context injection, output schema.

=========================================
GITHUB REPOSITORY STRUCTURE
=========================================
This is the recommended repo layout for GitHub Copilot to generate code into.
llm-remote/
?
??? backend/
?   ??? orchestrator/
?   ?   ??? src/
?   ?   ??? tests/
?   ?   ??? config/
?   ?
?   ??? mcp-sagetv/
?   ?   ??? src/
?   ?   ??? api/
?   ?   ??? tools/
?   ?   ??? tests/
?   ?
?   ??? mcp-channels/
?   ?   ??? src/
?   ?   ??? api/
?   ?   ??? tools/
?   ?   ??? tests/
?   ?
?   ??? mcp-linux/
?   ?   ??? src/
?   ?   ??? tools/
?   ?   ??? tests/
?   ?
?   ??? session-manager/
?   ?   ??? src/
?   ?   ??? tests/
?   ?
?   ??? transcription/
?       ??? watcher/
?       ??? queue/
?       ??? whisper/
?       ??? metadata/
?       ??? tests/
?
??? frontend/
?   ??? html5-remote/
?   ?   ??? public/
?   ?   ??? src/
?   ?   ?   ??? components/
?   ?   ?   ??? pages/
?   ?   ?   ??? hooks/
?   ?   ?   ??? state/
?   ?   ?   ??? api/
?   ?   ??? tests/
?
??? models/
?   ??? prompts/
?   ??? context/
?   ??? schemas/
?
??? docs/
?   ??? PRD/
?   ?   ??? 00-master-index.md
?   ?   ??? 01-prd.md
?   ?   ??? appendix-a-sagetv.md
?   ?   ??? appendix-b-channelsdvr.md
?   ?   ??? appendix-c-mcp.md
?   ?   ??? appendix-d-transcription.md
?   ?   ??? appendix-e-devices.md
?   ?   ??? appendix-f-ui.md
?   ?   ??? appendix-g-tools.md
?   ?   ??? appendix-h-prompt.md
?   ?
?   ??? architecture/
?   ??? api/
?   ??? deployment/
?
??? docker/
?   ??? orchestrator/
?   ??? mcp-sagetv/
?   ??? mcp-channels/
?   ??? mcp-linux/
?   ??? transcription/
?   ??? compose.yaml
?
??? scripts/
    ??? dev/
    ??? build/
    ??? deploy/

=========================================
README.md (TopLevel)
=========================================
Below is the full README.md GitHub Copilot should generate.

LLM Remote Control System for SageTV + ChannelsDVR
A unified HTML5 remote powered by a local LLM, local STT/TTS, and three MCP servers.
Supports naturallanguage and voice control of SageTV and ChannelsDVR across multiple playback devices.
Features
* HTML5 remote (mobile + desktop)
* Voice input (Whisper)
* Local LLM (Ollama or llama.cpp)
* Unified Session Manager
* Device registry + pairing
* SageTV MCP server
* ChannelsDVR MCP server
* Linux MCP server
* Transcription subsystem
* Responsible AI safety model
Architecture
See /docs/PRD/00-master-index.md.
Quick Start
docker compose up --build
Components
* /backend/orchestrator — LLM Gateway + tool routing
* /backend/mcp-sagetv — SageTV MCP server
* /backend/mcp-channels — ChannelsDVR MCP server
* /backend/mcp-linux — Linux MCP server
* /backend/session-manager — Device + session resolution
* /backend/transcription — Whisper + metadata
* /frontend/html5-remote — UI
License
MIT

=========================================
DEVELOPER ONBOARDING GUIDE
=========================================
This guide explains how a new developer sets up the environment.

1. Prerequisites
* Docker + Docker Compose
* Node.js 20+
* Python 3.11+
* Git
* SageTV server (optional for dev)
* ChannelsDVR server (optional for dev)

2. Clone the Repo
git clone https://github.com/<your-org>/llm-remote.git
cd llm-remote

3. Start Dev Environment
docker compose up --build
This launches:
* Orchestrator
* SageTV MCP
* ChannelsDVR MCP
* Linux MCP
* Transcription subsystem
* HTML5 remote

4. Frontend Dev Mode
cd frontend/html5-remote
npm install
npm run dev

5. Backend Dev Mode
Each MCP server can be run independently:
cd backend/mcp-sagetv
python src/main.py

6. Running Tests
pytest
npm test

7. Code Generation with GitHub Copilot
Copilot is expected to:
* Generate MCP tools
* Generate orchestrator routing
* Generate UI components
* Generate prompt logic
* Generate transcription workers

=========================================
BUILD & DEPLOYMENT GUIDE
=========================================
This guide defines how to build and deploy the full system.

1. Build All Containers
docker compose build

2. Run All Services
docker compose up -d

3. Environment Variables
Orchestrator
LLM_MODEL=llama3
MCP_SAGETV_URL=http://mcp-sagetv:8001
MCP_CHANNELS_URL=http://mcp-channels:8002
MCP_LINUX_URL=http://mcp-linux:8003
Transcription
WHISPER_MODEL=large-v3
TEMP_SSD_PATH=/mnt/ssd/tmp

4. Updating Models
ollama pull llama3
ollama pull whisper-large-v3

5. Production Deployment
Recommended:
* Docker Swarm
* Kubernetes
* Traefik or NGINX reverse proxy
* TLS termination
* Persistent volumes for: 
o Transcripts
o Device registry
o Logs

6. Monitoring
* Prometheus exporters
* Grafana dashboards
* Health endpoints for each MCP server

7. Logs
* Orchestrator logs
* MCP logs
* Transcription logs
* UI logs

8. Backup
Backup:
* Device registry
* Transcription metadata
* Docker volumes

Everything is now complete.
You now have:
* Full PRD
* All appendices (A–H)
* Master index
* GitHub repo structure
* README.md
* Developer onboarding guide
* Build/deployment guide

