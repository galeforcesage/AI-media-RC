=========================================
Appendix X -- Transcript Index SQL Schema
=========================================
This appendix defines the complete SQLite schema for the Transcript Indexing
and Cross-Metadata Reasoning Layer described in PRD Section 13.

X.1 Design Principles
* SQLite with FTS5 for full-text search
* Normalized tables for recordings, actors, transcript chunks
* System-agnostic: supports both SageTV and ChannelsDVR recordings
* Chunk size: 30-second windows with overlap for context
* All timestamps stored as Unix epoch seconds (integer)
* All text stored as UTF-8

X.2 Schema

------------------------------------------------------------------------
-- recordings: Normalized recording metadata from both systems
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recordings (
    recording_id    TEXT PRIMARY KEY,
    system          TEXT NOT NULL CHECK (system IN ('sagetv', 'channelsdvr')),
    title           TEXT NOT NULL,
    episode_title   TEXT,
    season          INTEGER,
    episode         INTEGER,
    genre           TEXT,           -- comma-separated genres
    channel         TEXT,
    channel_number  TEXT,
    air_date        INTEGER,        -- Unix epoch
    record_date     INTEGER,        -- Unix epoch
    duration        REAL,           -- seconds
    file_path       TEXT,
    file_size       INTEGER,        -- bytes
    description     TEXT,
    rating          TEXT,           -- e.g. "TV-PG", "TV-MA"
    source_id       TEXT,           -- native ID from SageTV or ChannelsDVR
    sidecar_path    TEXT,           -- path to .transcript.json
    transcribed_at  INTEGER,        -- Unix epoch when transcription completed
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_recordings_system ON recordings(system);
CREATE INDEX IF NOT EXISTS idx_recordings_title ON recordings(title);
CREATE INDEX IF NOT EXISTS idx_recordings_genre ON recordings(genre);
CREATE INDEX IF NOT EXISTS idx_recordings_channel ON recordings(channel);
CREATE INDEX IF NOT EXISTS idx_recordings_record_date ON recordings(record_date);
CREATE INDEX IF NOT EXISTS idx_recordings_air_date ON recordings(air_date);

------------------------------------------------------------------------
-- actors: Per-recording actor/cast list
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS actors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id    TEXT NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
    actor_name      TEXT NOT NULL,
    role            TEXT,           -- character name if available
    billing_order   INTEGER         -- 1 = lead, etc.
);

CREATE INDEX IF NOT EXISTS idx_actors_recording ON actors(recording_id);
CREATE INDEX IF NOT EXISTS idx_actors_name ON actors(actor_name COLLATE NOCASE);

------------------------------------------------------------------------
-- transcript_chunks: Timestamped transcript segments (30s windows)
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transcript_chunks (
    chunk_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id    TEXT NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,   -- 0-based sequential order
    start_time      REAL NOT NULL,      -- seconds from start
    end_time        REAL NOT NULL,      -- seconds from start
    text            TEXT NOT NULL,       -- transcript text for this chunk
    speaker         TEXT,               -- speaker label if diarization available
    confidence      REAL,               -- average word confidence 0.0-1.0
    word_count      INTEGER,
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_recording ON transcript_chunks(recording_id);
CREATE INDEX IF NOT EXISTS idx_chunks_time ON transcript_chunks(recording_id, start_time);

------------------------------------------------------------------------
-- FTS5 Virtual Table for full-text search over transcript chunks
------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    text,
    content='transcript_chunks',
    content_rowid='chunk_id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS5 in sync with transcript_chunks
CREATE TRIGGER IF NOT EXISTS transcript_chunks_ai AFTER INSERT ON transcript_chunks BEGIN
    INSERT INTO transcript_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS transcript_chunks_ad AFTER DELETE ON transcript_chunks BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS transcript_chunks_au AFTER UPDATE ON transcript_chunks BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
    INSERT INTO transcript_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;

------------------------------------------------------------------------
-- transcript_summaries: LLM-generated summaries per recording
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transcript_summaries (
    recording_id    TEXT PRIMARY KEY REFERENCES recordings(recording_id) ON DELETE CASCADE,
    summary         TEXT NOT NULL,
    keywords        TEXT,           -- JSON array of keywords
    topics          TEXT,           -- JSON array of topics
    generated_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

X.3 Query Examples

-- Full-text search with metadata join
SELECT r.title, r.episode_title, r.channel, tc.start_time, tc.end_time,
       snippet(transcript_fts, 0, '<b>', '</b>', '...', 32) AS snippet
FROM transcript_fts
JOIN transcript_chunks tc ON tc.chunk_id = transcript_fts.rowid
JOIN recordings r ON r.recording_id = tc.recording_id
WHERE transcript_fts MATCH 'climate change'
ORDER BY rank
LIMIT 20;

-- Search filtered by actor
SELECT r.title, r.episode_title, tc.start_time, tc.text
FROM transcript_fts
JOIN transcript_chunks tc ON tc.chunk_id = transcript_fts.rowid
JOIN recordings r ON r.recording_id = tc.recording_id
JOIN actors a ON a.recording_id = r.recording_id
WHERE transcript_fts MATCH 'chemistry'
  AND a.actor_name = 'Bryan Cranston'
ORDER BY rank;

-- Search filtered by genre and date range
SELECT r.title, tc.start_time, tc.text
FROM transcript_fts
JOIN transcript_chunks tc ON tc.chunk_id = transcript_fts.rowid
JOIN recordings r ON r.recording_id = tc.recording_id
WHERE transcript_fts MATCH 'vacation'
  AND r.genre LIKE '%Comedy%'
  AND r.record_date >= strftime('%s', 'now', '-7 days')
ORDER BY rank;

-- Find all recordings with a specific actor
SELECT r.recording_id, r.title, r.episode_title, a.role
FROM actors a
JOIN recordings r ON r.recording_id = a.recording_id
WHERE a.actor_name LIKE '%cranston%'
ORDER BY r.record_date DESC;

X.4 Migration Notes
* Schema version tracked in a separate meta table (not shown)
* All tables use ON DELETE CASCADE for clean recording removal
* FTS5 rebuild: INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild');
* Expected index size: ~1KB per chunk, ~100 chunks per hour of content
* 5000 recordings x 1 hour avg = ~500K chunks = ~500MB index

End of Appendix X
