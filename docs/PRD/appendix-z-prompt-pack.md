=========================================
Appendix Z -- Copilot Prompt Pack for Transcript Modules
=========================================
This appendix provides structured prompt templates that GitHub Copilot should
use when generating the Transcript Indexing and Cross-Metadata Reasoning code.

Each prompt defines the module, its purpose, inputs, outputs, dependencies,
and the authoritative specification it must conform to.

Z.1 Module: transcript_index.py (Database Layer)

PROMPT:
Generate a Python module `transcript_index.py` that manages the SQLite
transcript index database.

Requirements:
- Use the exact SQL schema from Appendix X (appendix-x-sql-schema.md)
- Class: TranscriptIndex
- Constructor takes db_path: str, creates tables on init
- Methods:
  * insert_recording(recording: dict) -> None
  * insert_actors(recording_id: str, actors: list[dict]) -> None
  * insert_chunks(recording_id: str, chunks: list[dict]) -> None
  * insert_summary(recording_id: str, summary: dict) -> None
  * delete_recording(recording_id: str) -> None
  * get_recording(recording_id: str) -> dict | None
  * search_transcripts(query: str, filters: dict | None) -> list[dict]
    - filters supports: actor, genre, channel, date_from, date_to, system
    - Returns: list of {recording_id, title, episode_title, channel,
      chunk_index, start_time, end_time, snippet, rank}
  * search_by_actor(actor_name: str) -> list[dict]
  * get_stats() -> dict  (total recordings, total chunks, etc.)
  * rebuild_fts() -> None
- Use context manager for transactions
- Use parameterized queries (no string interpolation)
- All public methods have logging via utils.logger

Dependencies: sqlite3, logging
Conforms to: Appendix X SQL Schema

Z.2 Module: sidecar.py (JSON Sidecar Reader/Writer)

PROMPT:
Generate a Python module `sidecar.py` that reads and writes transcript
JSON sidecar files.

Requirements:
- Conform to the JSON schema in Appendix Y (appendix-y-json-sidecar.md)
- Class: TranscriptSidecar
- Constructor takes output_dir: str
- Methods:
  * write(recording_id: str, system: str, metadata: dict,
          transcript: dict, chunks: list[dict],
          summary: dict | None = None) -> str
    - Returns the full path to the written .transcript.json file
    - Validates required fields before writing
  * read(path: str) -> dict
    - Reads and validates a sidecar file
    - Returns the parsed dict
  * find_sidecars(directory: str) -> list[str]
    - Recursively finds all .transcript.json files
  * reindex_all(directory: str, index: TranscriptIndex) -> int
    - Reads all sidecars and inserts into the transcript index
    - Returns count of recordings indexed
- Use json module (no external deps)
- Atomic writes (write to .tmp then rename)

Dependencies: json, os, pathlib, tempfile
Conforms to: Appendix Y JSON Sidecar Schema

Z.3 Module: enrichment.py (Metadata Enrichment Pipeline)

PROMPT:
Generate a Python module `enrichment.py` that runs the post-transcription
metadata enrichment pipeline.

Requirements:
- Class: MetadataEnrichmentPipeline
- Constructor takes:
  * index: TranscriptIndex
  * sidecar: TranscriptSidecar
  * sagetv_url: str  (MCP SageTV endpoint)
  * channels_url: str  (MCP Channels DVR endpoint)
- Methods:
  * async enrich(job: dict) -> None
    Main pipeline entry point. Called after transcription completes.
    Steps:
    1. Determine system from job
    2. Fetch recording metadata from appropriate MCP server
       - SageTV: JSON-RPC call to get_recording_metadata
       - ChannelsDVR: JSON-RPC call to get_recording_details
    3. Extract actors list from metadata
    4. Split transcript into 30-second chunks with timestamps
    5. Write JSON sidecar via sidecar.write()
    6. Insert recording, actors, chunks into index
  * chunk_transcript(segments: list[dict], window: int = 30) -> list[dict]
    - Takes Whisper segments (with start, end, text)
    - Groups into windows of `window` seconds
    - Returns list of {index, start_time, end_time, text, word_count}
  * async fetch_metadata(system: str, recording_id: str) -> dict
    - JSON-RPC call to appropriate MCP server
- All methods have error handling and logging
- Pipeline failures should not block the transcription worker

Dependencies: aiohttp, json, logging
Conforms to: Appendix X + Y schemas

Z.4 Module: search_service.py (Cross-Metadata Search)

PROMPT:
Generate a Python module `search_service.py` that provides the
cross-metadata search API.

Requirements:
- Class: TranscriptSearchService
- Constructor takes index: TranscriptIndex
- Methods:
  * search(query: str, filters: dict | None = None,
           limit: int = 20, offset: int = 0) -> dict
    - Delegetes to index.search_transcripts()
    - Returns: {results: [...], total: int, query: str, filters: dict}
  * search_actor(actor_name: str, limit: int = 20) -> dict
    - Returns recordings featuring the actor
  * get_recording_summary(recording_id: str) -> dict
    - Returns full enriched summary (metadata + transcript summary + actors)
  * suggest_context(query: str, max_chunks: int = 5) -> str
    - Searches transcripts and formats top results as LLM context string
    - Format: "From {title} S{season}E{episode} at {time}: {snippet}"
- This module is the interface consumed by the orchestrator's LLM service

Dependencies: transcript_index
Conforms to: PRD Section 13.2.4

Z.5 Module: MCP Tools (tools.py additions)

PROMPT:
Add transcript search tools to the transcription MCP server's tools.py.

New tools to add:
1. transcript_search
   - params: query (str), actor (str|null), genre (str|null),
     channel (str|null), date_from (str|null), date_to (str|null),
     limit (int, default 20)
   - Returns: ranked search results with snippets and metadata

2. transcript_actors
   - params: actor_name (str)
   - Returns: list of recordings featuring the actor

3. transcript_recording_summary
   - params: recording_id (str)
   - Returns: full enriched summary for a recording

4. transcript_reindex
   - params: directory (str|null)
   - Returns: count of recordings reindexed
   - Safety level: CONFIRM

Register all tools with the MCP server tool registry.
Each tool should validate inputs and handle errors gracefully.

Dependencies: search_service, transcript_index
Conforms to: PRD Section 13.2.4, Appendix G tool format

Z.6 Module: Orchestrator Integration (services/search.py updates)

PROMPT:
Update the orchestrator's services/search.py to integrate transcript
search results into LLM context.

Requirements:
- Add method: async transcript_search(query: str, filters: dict) -> dict
  * Calls the transcription MCP server's transcript_search tool
- Add method: async inject_transcript_context(query: str) -> str
  * Searches transcripts for relevant content
  * Formats top 5 results as context string for LLM injection
  * Called by the LLM service before generating a response
- Update existing search flow to include transcript results when relevant

Dependencies: transport.mcp (for MCP calls to transcription server)
Conforms to: PRD Section 13.2.5

Z.7 Generation Order
Copilot should generate modules in this order:
1. transcript_index.py (database layer - no external deps)
2. sidecar.py (file I/O - depends on transcript_index types)
3. enrichment.py (pipeline - depends on index + sidecar + MCP)
4. search_service.py (search API - depends on index)
5. tools.py updates (MCP exposure - depends on search_service)
6. services/search.py updates (orchestrator integration)

End of Appendix Z
