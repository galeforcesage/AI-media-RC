=========================================
Appendix AA -- Test Plan for Transcript-Metadata Reasoning
=========================================
This appendix defines the complete test plan for the Transcript Indexing
and Cross-Metadata Reasoning Layer described in PRD Section 13.

AA.1 Test Categories
1. Ingestion Tests
2. Index Building Tests
3. Metadata Extraction Tests
4. Full-Text Search Tests
5. Cross-Metadata Reasoning Tests
6. Actor Filtering Tests
7. Sidecar File Tests
8. Enrichment Pipeline Tests
9. MCP Tool Tests
10. Orchestrator Integration Tests
11. Error Handling & Edge Cases
12. Performance Tests

AA.2 Test Data
Seed data for all tests:
* 5 recordings from SageTV (mixed genres: drama, comedy, news)
* 5 recordings from ChannelsDVR (mixed genres: documentary, sports, sitcom)
* Each recording with 2-5 actors
* Each recording with 50-200 transcript chunks (simulating 25-100 min content)
* At least 2 recordings sharing an actor (for cross-recording actor queries)

AA.3 Ingestion Tests

T-ING-01: Insert single SageTV recording
  Given: A completed transcription job for a SageTV recording
  When: The enrichment pipeline runs
  Then: Recording appears in the recordings table with correct metadata

T-ING-02: Insert single ChannelsDVR recording
  Given: A completed transcription job for a ChannelsDVR recording
  When: The enrichment pipeline runs
  Then: Recording appears in the recordings table with system='channelsdvr'

T-ING-03: Insert actors for a recording
  Given: A recording with metadata containing 3 actors
  When: Actors are inserted
  Then: 3 rows in actors table linked to the recording_id

T-ING-04: Insert transcript chunks
  Given: A 30-minute recording transcribed into ~60 chunks
  When: Chunks are inserted
  Then: 60 rows in transcript_chunks, FTS5 index contains all text

T-ING-05: Duplicate recording insert
  Given: A recording_id that already exists
  When: Insert is attempted again
  Then: Graceful handling (upsert or error with message)

T-ING-06: Delete recording cascades
  Given: A recording with actors and chunks
  When: Recording is deleted
  Then: All actors and chunks for that recording are also deleted

AA.4 Index Building Tests

T-IDX-01: FTS5 index builds on insert
  Given: Chunks inserted for a recording
  When: FTS5 search is performed for a word in the chunks
  Then: Results returned with correct chunk_id and rank

T-IDX-02: FTS5 index updates on chunk update
  Given: A chunk is updated with new text
  When: FTS5 search is performed
  Then: Old text not found, new text found

T-IDX-03: FTS5 rebuild
  Given: An existing index with data
  When: rebuild_fts() is called
  Then: FTS5 index is rebuilt and searches work correctly

T-IDX-04: Stats accuracy
  Given: 10 recordings with 500 total chunks
  When: get_stats() is called
  Then: Returns {total_recordings: 10, total_chunks: 500, ...}

AA.5 Metadata Extraction Tests

T-META-01: SageTV metadata fetch
  Given: A SageTV recording_id
  When: fetch_metadata('sagetv', recording_id) is called
  Then: Returns dict with title, episode, genre, actors, channel, dates

T-META-02: ChannelsDVR metadata fetch
  Given: A ChannelsDVR recording_id
  When: fetch_metadata('channelsdvr', recording_id) is called
  Then: Returns dict with title, episode, genre, channel, dates

T-META-03: Metadata fetch failure
  Given: An invalid recording_id
  When: fetch_metadata is called
  Then: Returns None or raises handled exception, logged

T-META-04: Actor extraction
  Given: Metadata with actors array
  When: Actors are extracted and inserted
  Then: Each actor appears with correct name, role, billing_order

AA.6 Full-Text Search Tests

T-FTS-01: Simple keyword search
  Given: Chunks containing "climate change" in 3 recordings
  When: search("climate change") is called
  Then: Returns 3+ results ranked by relevance

T-FTS-02: Phrase search
  Given: Chunks containing exact phrase "to be or not to be"
  When: search('"to be or not to be"') is called
  Then: Returns only chunks with the exact phrase

T-FTS-03: Multi-word search with ranking
  Given: Chunks with varying relevance to "Walter chemistry lab"
  When: search("Walter chemistry lab") is called
  Then: Results ordered by FTS5 rank (most relevant first)

T-FTS-04: No results
  Given: No chunks containing "xylophone supernova"
  When: search("xylophone supernova") is called
  Then: Returns empty list, no error

T-FTS-05: Search with snippet generation
  Given: A matching chunk
  When: Search returns results
  Then: Each result includes a snippet with highlighted match terms

AA.7 Cross-Metadata Reasoning Tests

T-CMR-01: Text search + genre filter
  Given: "vacation" appears in Comedy and Drama recordings
  When: search("vacation", filters={genre: "Comedy"})
  Then: Returns only Comedy recordings

T-CMR-02: Text search + channel filter
  Given: "budget" appears on multiple channels
  When: search("budget", filters={channel: "1045"})
  Then: Returns only channel 1045 results

T-CMR-03: Text search + date range filter
  Given: Recordings from multiple dates
  When: search("news", filters={date_from: "2025-01-10", date_to: "2025-01-17"})
  Then: Returns only recordings within the date range

T-CMR-04: Text search + actor filter
  Given: "chemistry" in Breaking Bad (Bryan Cranston) and a chemistry documentary
  When: search("chemistry", filters={actor: "Bryan Cranston"})
  Then: Returns only Breaking Bad chunks

T-CMR-05: Text search + system filter
  Given: Same show recorded on both systems
  When: search("episode", filters={system: "sagetv"})
  Then: Returns only SageTV recordings

T-CMR-06: Combined filters (actor + genre + date)
  Given: Multiple matching criteria
  When: search("scene", filters={actor: "Tom Hanks", genre: "Drama", date_from: "2025-01-01"})
  Then: Returns intersection of all filters

AA.8 Actor Filtering Tests

T-ACT-01: Find recordings by actor
  Given: Actor "Bryan Cranston" in 3 recordings
  When: search_actor("Bryan Cranston") is called
  Then: Returns 3 recordings with titles and roles

T-ACT-02: Actor name case insensitive
  Given: Actor stored as "Bryan Cranston"
  When: search_actor("bryan cranston") is called
  Then: Returns the same results

T-ACT-03: Actor with partial name
  Given: Actor "Bryan Cranston"
  When: search_actor("Cranston") is called
  Then: Returns matching results (LIKE search)

T-ACT-04: Actor not found
  Given: No actor named "Nonexistent Person"
  When: search_actor("Nonexistent Person") is called
  Then: Returns empty list

AA.9 Sidecar File Tests

T-SC-01: Write sidecar
  Given: Complete recording data (metadata, transcript, chunks)
  When: sidecar.write() is called
  Then: .transcript.json file created with valid JSON matching schema

T-SC-02: Read sidecar
  Given: A valid .transcript.json file
  When: sidecar.read() is called
  Then: Returns parsed dict with all expected fields

T-SC-03: Atomic write
  Given: Sidecar write in progress
  When: Process is interrupted
  Then: No partial .transcript.json file exists (temp file only)

T-SC-04: Find sidecars recursively
  Given: Directory tree with 5 .transcript.json files at various depths
  When: find_sidecars() is called
  Then: Returns all 5 paths

T-SC-05: Reindex from sidecars
  Given: Empty transcript index + 5 valid sidecar files
  When: reindex_all() is called
  Then: All 5 recordings with actors and chunks appear in the index

T-SC-06: Invalid sidecar skipped
  Given: A corrupt .transcript.json file among valid ones
  When: reindex_all() is called
  Then: Valid files indexed, corrupt file logged as error and skipped

AA.10 Enrichment Pipeline Tests

T-ENR-01: Full pipeline SageTV
  Given: Completed transcription job for SageTV recording
  When: enrich() is called
  Then: Metadata fetched, sidecar written, index populated

T-ENR-02: Full pipeline ChannelsDVR
  Given: Completed transcription job for ChannelsDVR recording
  When: enrich() is called
  Then: Metadata fetched, sidecar written, index populated

T-ENR-03: Chunking produces correct windows
  Given: Whisper segments spanning 90 seconds
  When: chunk_transcript(segments, window=30) is called
  Then: Returns 3 chunks: [0-30s, 30-60s, 60-90s]

T-ENR-04: Pipeline failure does not block transcription
  Given: MCP metadata fetch fails
  When: enrich() is called
  Then: Error logged, transcription pipeline continues

T-ENR-05: Pipeline with missing actors
  Given: Recording metadata with no actors field
  When: enrich() is called
  Then: Recording and chunks indexed, actors table empty for this recording

AA.11 MCP Tool Tests

T-MCP-01: transcript_search tool
  Given: Indexed recordings
  When: MCP call transcript_search(query="test", limit=5)
  Then: Returns up to 5 ranked results

T-MCP-02: transcript_actors tool
  Given: Actors in the index
  When: MCP call transcript_actors(actor_name="Cranston")
  Then: Returns recordings list

T-MCP-03: transcript_recording_summary tool
  Given: An indexed recording with summary
  When: MCP call transcript_recording_summary(recording_id="...")
  Then: Returns full enriched summary

T-MCP-04: transcript_reindex tool
  Given: Sidecar files on disk
  When: MCP call transcript_reindex()
  Then: Returns count of reindexed recordings

T-MCP-05: Invalid tool params
  Given: Missing required parameter
  When: MCP call transcript_search()
  Then: Returns error response with message

AA.12 Orchestrator Integration Tests

T-ORC-01: Transcript context injection
  Given: User query "what did they say about the budget?"
  When: inject_transcript_context() is called
  Then: Returns formatted context string with relevant snippets

T-ORC-02: LLM receives transcript context
  Given: A query that matches transcript content
  When: Full LLM pipeline runs
  Then: LLM response references specific episodes and timestamps

T-ORC-03: No transcript results
  Given: Query with no matching transcripts
  When: inject_transcript_context() is called
  Then: Returns empty string, LLM proceeds without transcript context

AA.13 Error Handling & Edge Cases

T-ERR-01: Empty transcript
  Given: Recording with 0-length audio
  When: Enrichment pipeline runs
  Then: Recording inserted with 0 chunks, no errors

T-ERR-02: Very long recording (6+ hours)
  Given: A 6-hour recording with 720+ chunks
  When: Indexed and searched
  Then: All chunks indexed, search returns results normally

T-ERR-03: Unicode in transcript
  Given: Transcript with non-ASCII characters (accents, CJK, emoji)
  When: Indexed and searched
  Then: FTS5 handles unicode correctly

T-ERR-04: Concurrent index writes
  Given: Two enrichment pipelines running simultaneously
  When: Both attempt to insert
  Then: No database corruption (WAL mode)

T-ERR-05: Database file missing
  Given: TranscriptIndex initialized with nonexistent path
  When: Constructor runs
  Then: Creates new database with full schema

T-ERR-06: SQL injection via search query
  Given: Malicious search query with SQL characters
  When: search("'; DROP TABLE recordings; --") is called
  Then: Parameterized query prevents injection, returns 0 results

AA.14 Performance Tests

T-PERF-01: Index insert speed
  Given: A recording with 200 chunks
  When: Full insert (recording + actors + chunks)
  Then: Completes in < 500ms

T-PERF-02: FTS5 search speed (small index)
  Given: 100 recordings, 10K chunks
  When: search("keyword")
  Then: Returns in < 100ms

T-PERF-03: FTS5 search speed (large index)
  Given: 5000 recordings, 500K chunks
  When: search("keyword", filters={genre: "Drama"})
  Then: Returns in < 200ms

T-PERF-04: Reindex speed
  Given: 100 sidecar files
  When: reindex_all() is called
  Then: Completes in < 30 seconds

End of Appendix AA
