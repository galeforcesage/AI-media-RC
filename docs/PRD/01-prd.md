=========================================
01-prd.md � Unified Product Requirements Document
=========================================
LLM Remote Control System for SageTV + ChannelsDVR
Version: 1.0
Author: T
Target Consumer: GitHub Copilot (primary), developers (secondary)
Status: Authoritative PRD

0. Executive Summary
This PRD defines the complete requirements and architecture for a unified HTML5 LLM Remote Control System supporting SageTV and ChannelsDVR as equal firstclass systems.
The system enables:
* Naturallanguage control
* Voice control (Whisper STT)
* Local LLM intent parsing
* Multiroom device binding
* Sessionaware playback control
* DVR management
* EPG search
* Recording transcription
* Localonly privacy
The solution uses:
* HTML5 remote
* Unified Session Manager
* Device Registry
* Three MCP servers (SageTV, ChannelsDVR, Linux)
* Local LLM (Ollama or llama.cpp)
* Local STT/TTS
* Transcription subsystem
* Responsible AI safety model
This PRD is written explicitly for GitHub Copilot to generate the full implementation.

1. Goals & Objectives
Primary Goals
* Provide a modern, voiceenabled remote for SageTV + ChannelsDVR
* Support naturallanguage commands via local LLM
* Maintain strict privacy (no cloud LLM/STT/TTS)
* Provide deterministic, sessionaware playback control
* Support multidevice, multiroom households
* Provide full DVR management
* Provide recording transcription and metadata search
NonGoals
* Replacing MiniClient or Placeshifter
* Streaming video
* Cloudbased features
* Multiuser permission systems

2. System Architecture Overview
The system consists of:
* HTML5 Remote (UI)
* LLM Gateway
* Unified Session Manager
* Device Registry
* SageTV MCP Server
* ChannelsDVR MCP Server
* Linux MCP Server
* Transcription Subsystem
* Local LLM + STT/TTS
HighLevel Flow
User ? HTML5 Remote ? STT ? LLM Gateway ? MCP Servers ? SageTV/ChannelsDVR

3. HTML5 Remote Requirements
Core Features
* Device picker
* System picker
* Voice input (pressandhold)
* Text input
* LLM response bubbles
* Transport controls
* Now Playing panel
* Admin panel
UI Regions
1. Header (system + device picker)
2. Now Playing
3. Transport controls
4. Voice/Text input
5. Footer (settings/admin)
Mobile/desktop support
Responsive layout required.

4. Device Registry
Purpose
* Bind remote ? device
* Provide device metadata
* Support pairing
* Support multiroom control
Pairing Methods
* SageTV QR code
* ChannelsDVR API enumeration
* Manual pairing
Device Limit
* Default: 15
* Hard limit: 50

5. Unified Session Manager
Responsibilities
* Resolve active playback session
* Map device_id ? client_id ? session_id
* Provide playback context
* Cache session state
Session Resolution
* SageTV: via MediaPlayerAPI + client list
* ChannelsDVR: via /sessions + /clients

6. STT/TTS Subsystem
STT
* Whisper Largev3 (preferred)
* Automatic model selection based on RAM
* Browser STT fallback
TTS
* Local TTS (Kokoro recommended)

7. LLM Gateway
Responsibilities
* Inject context (device, session, playback)
* Apply system prompt
* Route tool calls
* Enforce safety levels
* Handle multiturn confirmations
Output Format
Structured JSON:
{
  "intent": "...",
  "response_text": "...",
  "tool": "...",
  "tool_args": {...},
  "confirmation_required": false
}

8. MCP Architecture
Three MCP Servers
1. SageTV MCP
2. ChannelsDVR MCP
3. Linux MCP
Orchestrator
* Merges tool registries
* Routes tool calls
* Validates safety
* Handles errors

9. Transcription Subsystem
Components
* File watchers
* SSD extraction worker
* Transcription queue
* Whisper engine
* Metadata generator
* Metadata store
* MCP resource exposure
Outputs
* Transcript
* Summary
* Keywords
* Topics
* Scene boundaries

10. Security Model
Authentication
* Login required for remote
* Ownerlevel actions require elevated session
Responsible AI
Four safety levels:
* SAFE
* CONFIRM
* DANGEROUS
* OWNER
Privacy
* No cloud LLM
* No cloud STT/TTS
* Localonly processing

11. Functional Requirements
Playback
* Play/pause/stop
* Skip forward/back
* Seek
* Volume
* Mute
* Channel tuning
* Commercial skip (ChannelsDVR)
DVR
* Schedule recording
* Cancel recording
* Delete recording
* Manage favorites
Search
* EPG search
* Recording search
* Transcript search

12. NonFunctional Requirements
* Latency < 3 seconds
* Localonly privacy
* Modular architecture
* Dockerbased deployment
* High maintainability

13. Transcript Indexing and Cross-Metadata Reasoning Layer

13.1 Purpose
Extend the transcription subsystem with a structured index that links transcript
segments to recording metadata (actors, genres, channels, air dates, ratings) from
both SageTV and ChannelsDVR. This enables the LLM to answer cross-domain
natural-language queries such as:
* "Which episodes mention climate change?"
* "Find scenes with Bryan Cranston talking about chemistry"
* "What did the host say about the budget on channel 1045 last Tuesday?"
* "Show all comedies recorded this week where someone says 'vacation'"

13.2 Components
1. Transcript Index (SQLite + FTS5)
   * recordings table: normalized recording metadata from both systems
   * actors table: per-recording actor/cast list
   * transcript_chunks table: timestamped transcript segments (30s windows)
   * FTS5 virtual table over transcript_chunks for full-text search
   * See Appendix X for complete SQL schema

2. JSON Sidecar Files
   * One .transcript.json per recording, written alongside the .txt and .vtt
   * Contains raw transcript, cleaned transcript, timestamps, speaker tags,
     LLM-generated summary, chunk boundaries, and optional embeddings
   * See Appendix Y for JSON schema

3. Metadata Enrichment Pipeline
   * After transcription completes, the pipeline:
     a. Fetches recording metadata from the appropriate MCP server
     b. Extracts actor/cast list
     c. Splits transcript into 30-second chunks with timestamps
     d. Writes the JSON sidecar
     e. Inserts into the transcript index (recordings + actors + chunks)
   * Pipeline runs as a post-transcription hook in the worker

4. Cross-Metadata Search Service
   * Accepts structured queries combining transcript text + metadata filters
   * Supports: text search, actor filter, genre filter, channel filter, date range
   * Returns ranked results with snippet context and timestamps
   * Exposed via MCP tools:
     - transcript_search(query, filters) -> ranked chunks with metadata
     - transcript_actors(actor_name) -> recordings featuring actor
     - transcript_summary(recording_id) -> full enriched summary

5. LLM Reasoning Integration
   * The orchestrator injects search results as context for the LLM
   * The LLM can chain: parse intent -> search transcripts -> filter by metadata
     -> format response with timestamps and episode references
   * See Appendix Z for Copilot prompt pack

13.3 Data Flow
[Transcription Complete]
   |
[Metadata Enrichment Pipeline]
   |
   +-- Fetch metadata from MCP SageTV / MCP ChannelsDVR
   +-- Extract actors, genre, channel, air date
   +-- Split transcript into 30s chunks
   +-- Write JSON sidecar
   +-- Insert into transcript index
   |
[Cross-Metadata Search Service]
   |
   +-- FTS5 text search over chunks
   +-- JOIN with recordings + actors for metadata filters
   +-- Return ranked results with snippets
   |
[LLM Orchestrator]
   |
   +-- Inject search results as context
   +-- Generate natural-language response

13.4 Performance Requirements
* Index insert: < 500ms per recording (excluding transcription time)
* FTS5 search: < 200ms for typical queries
* Metadata fetch: use cached MCP responses where available
* Sidecar write: async, non-blocking to transcription pipeline

13.5 Test Plan
See Appendix AA for the complete test plan covering ingestion, indexing,
search, actor filtering, cross-metadata reasoning, and edge cases.

14. Appendices
* Appendix A -- SageTV Capability Dictionary
* Appendix B -- ChannelsDVR Capability Dictionary
* Appendix C -- MCP Architecture
* Appendix D -- Transcription Subsystem
* Appendix E -- Device Registry
* Appendix F -- HTML5 Remote UI Spec
* Appendix G -- MCP Tool Definitions
* Appendix H -- LLM Prompt Specification
* Appendix X -- Transcript Index SQL Schema
* Appendix Y -- Transcript JSON Sidecar Schema
* Appendix Z -- Copilot Prompt Pack for Transcript Modules
* Appendix AA -- Test Plan for Transcript-Metadata Reasoning

End of PRD


