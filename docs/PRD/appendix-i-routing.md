=========================================
Appendix I — Unified Routing Model (SageTV + ChannelsDVR + Linux + Transcripts)
=========================================

This appendix defines how the orchestrator's agent loop selects the correct
MCP server(s) for any given user request. It is the authoritative reference
for the system prompt embedded in `backend/orchestrator/src/services/agent.py`.

I.1 Architecture Overview
-------------------------

The agent controls four MCP servers:

| # | Server           | Prefix        | Port | Purpose                        |
|---|------------------|---------------|------|--------------------------------|
| 1 | SageTV MCP       | `sagetv_`     | 8766 | DVR engine (SageTV backend)    |
| 2 | ChannelsDVR MCP  | `channels_`   | 8767 | DVR engine (Channels backend)  |
| 3 | Linux MCP        | `linux_`      | 8768 | Filesystem & system helper     |
| 4 | Transcript Index | `transcript_` | 8770 | Semantic search over captions  |

SageTV MCP and ChannelsDVR MCP are **functionally identical DVRs**. They
differ only in which backend they target. All DVR logic applies equally
to both.


I.2 MCP Server Selection (Routing Rules)
-----------------------------------------

### USE A DVR MCP (SageTV or ChannelsDVR) FOR:
- Recording metadata, search, listing, filtering
- Recording deletion
- Recording playback (play, pause, resume, seek, skip, commercial skip)
- Airings / EPG queries
- Favorites / passes / rules
- Retrieving recording file paths
- Recording move/archive (if supported by backend)
- DVR-level reindex / rescan
- Transcript → recording mapping (once recording_id is known)
- Any action involving a show, episode, or recording

### USE LINUX MCP ONLY FOR:
- Directory listing
- Disk usage
- File size / file existence checks
- Reading raw files
- Moving/copying files **only** if the DVR does not support it
- Deleting **only** orphaned files (not DVR-managed)
- Validating file paths returned by DVR MCPs
- Counting files by glob pattern
- Service/container status, logs, restarts
- Server-level operations (reboot, shutdown)

### USE TRANSCRIPT INDEX FOR:
- Semantic search across captions
- Quotes, scenes, characters, dialogue queries
- "Which episode has the line…"
- "Find the scene where…"
- Mapping transcript results → recording → DVR metadata


I.3 Entity Extraction
---------------------

When the user mentions a show, episode, or recording:

1. **Reduce** to minimal search text (e.g., "big bang theory", "law order svu")
2. **Search** using the DVR MCP's search tool
3. **Select** canonical match from results
4. **Use** canonical IDs in the final tool call
5. **If ambiguous**, ask the user for clarification

Normalize:
- Lowercase
- Strip punctuation
- Remove filler words ("the", "a", "an")
- Keep only core title for search
- For transcripts: extract semantic keywords


I.4 Transcript-Aware Routing
------------------------------

If the user references quotes, scenes, characters, or dialogue:

1. Query `transcript_search` or `transcript_cross_search`
2. Resolve transcript result → `recording_id` + `timestamp`
3. Use the correct DVR MCP to act on that recording

**Examples:**

| User says                              | Action                                          |
|----------------------------------------|------------------------------------------------|
| "Play the scene where he says goodbye" | transcript_search → resolve → dvr.play(offset) |
| "Delete the episode with the car chase"| transcript_search → resolve → dvr.delete(id)   |
| "Who was in that episode?"             | transcript_search → resolve → dvr.get_recording|


I.5 Linux Path Resolution (Helper Only)
-----------------------------------------

**Linux MCP is never used to delete DVR-managed files.**

Linux MCP is used to:
- Inspect directories
- Confirm file existence
- Get file sizes
- Read raw files
- Move/copy files only if DVR cannot
- Delete orphaned files only

**If a path is unknown:**
- Retrieve it from DVR metadata (e.g., `channels_get_storage_status`)
- Never hallucinate paths

**Dynamic path discovery:**
The agent pre-discovers recording paths at the start of each session by
calling `channels_get_storage_status` and injecting the result into the
system prompt. SageTV's path (`/var/media/tv`) is static.


I.6 Multi-MCP Orchestration Patterns
--------------------------------------

The agent may chain multiple MCP calls in a single session:

### A) Delete Recording (DVR-safe)
1. Minimal show search (DVR MCP)
2. Canonicalize
3. Find recording
4. Delete via DVR MCP

### B) Delete File Safely
1. Extract path
2. Ask SageTV MCP if file is DVR-managed
3. Ask ChannelsDVR MCP if file is DVR-managed
4. If DVR-managed → delete via DVR MCP
5. If not → delete via Linux MCP

### C) Move Recording File
1. Minimal show search (DVR MCP)
2. Canonicalize
3. Get recording metadata
4. If DVR supports move/archive → use DVR MCP
5. Else: Linux MCP to move file → Trigger DVR rescan

### D) Transcript-Based Playback
1. `transcript_search` → find matching segment
2. Resolve recording + timestamp
3. DVR MCP → `play_recording(offset_seconds=T)`

### E) Transcript-Based Deletion
1. `transcript_search` → find matching recording
2. Resolve `recording_id`
3. DVR MCP → `delete_recording(id)`

### F) File Size of DVR Recording
1. Minimal show search (DVR MCP)
2. Canonicalize
3. Get recording metadata → extract file path
4. `linux_file_info(path)` → return size

### G) Orphaned File Cleanup
1. Get DVR-managed file lists from both DVR MCPs
2. Get filesystem listing from Linux MCP
3. Compute set difference (orphans)
4. Delete orphans via Linux MCP


I.7 Safety Levels
------------------

All tools have assigned safety levels:

| Level     | Description                       | Agent behavior            |
|-----------|-----------------------------------|---------------------------|
| SAFE      | Read-only, no side effects        | Execute freely             |
| CONFIRM   | Modifies state (delete, schedule) | Warn user before executing |
| DANGEROUS | Server reboot, shutdown           | Only if user requests it   |
| OWNER     | Config changes, cache clear       | Only if user requests it   |

The agent **never** executes DANGEROUS or OWNER tools proactively.


I.8 General Rules
------------------

1. Never delete DVR-managed files via Linux MCP.
2. Always verify show names via minimal-text search.
3. Always canonicalize show names and IDs.
4. Always use transcript index for semantic queries.
5. Always use DVR MCP for any recording lifecycle operation.
6. Linux MCP is helper-only unless explicitly asked to operate on non-DVR files.
7. Ask for clarification when DVR target is ambiguous (SageTV vs ChannelsDVR).
8. Never hallucinate file paths — retrieve from metadata or discovered paths.
9. If a tool returns an error, do NOT retry — tell the user the service is offline.


I.9 Tool Inventory
-------------------

Complete count by server:

| Server           | Total | Categories                                       |
|------------------|-------|--------------------------------------------------|
| SageTV MCP       |  46   | Query(15), Playback(13), Nav(4), Mgmt(5), Fav(2), Config(5), Events(2) |
| ChannelsDVR MCP  |  24   | Query(8), Playback(8), Mgmt(6), System(2)        |
| Linux MCP        |  17   | Info(4), Files(4), Services(6), Control(3)       |
| Transcript Index |   8   | Search(3), Stats(1), Lookup(2), Admin(2)         |
| **Total**        | **95**|                                                  |

See `backend/orchestrator/src/services/agent.py` TOOL_DEFINITIONS for the
full tool reference with parameters.
