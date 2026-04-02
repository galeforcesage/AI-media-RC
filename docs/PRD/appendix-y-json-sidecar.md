=========================================
Appendix Y -- Transcript JSON Sidecar Schema
=========================================
This appendix defines the JSON schema for per-recording transcript sidecar
files (.transcript.json) as described in PRD Section 13.

Y.1 Purpose
Each transcribed recording produces a .transcript.json sidecar file stored
alongside the .txt and .vtt outputs. This file is the authoritative source
for enriched transcript data and is consumed by:
* The metadata enrichment pipeline (for index insertion)
* The LLM context system (for injecting transcript context)
* External tools that read sidecar files directly

Y.2 File Naming Convention
* Sidecar file: <recording_filename>.transcript.json
* Example: Breaking_Bad_S02E03_20250101.mpg.transcript.json
* Stored in the same directory as the transcript .txt and .vtt files
* Location: configurable transcript output directory

Y.3 Schema Definition

{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "TranscriptSidecar",
  "description": "Per-recording transcript sidecar with metadata, chunks, and summaries",
  "type": "object",
  "required": ["version", "recording_id", "system", "metadata", "transcript", "chunks"],
  "properties": {
    "version": {
      "type": "string",
      "description": "Schema version",
      "const": "1.0"
    },
    "recording_id": {
      "type": "string",
      "description": "Unique recording ID (matches recordings table)"
    },
    "system": {
      "type": "string",
      "enum": ["sagetv", "channelsdvr"],
      "description": "Source system"
    },
    "metadata": {
      "type": "object",
      "description": "Recording metadata fetched from MCP server",
      "required": ["title"],
      "properties": {
        "title": { "type": "string" },
        "episode_title": { "type": ["string", "null"] },
        "season": { "type": ["integer", "null"] },
        "episode": { "type": ["integer", "null"] },
        "genre": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Genre list"
        },
        "channel": { "type": ["string", "null"] },
        "channel_number": { "type": ["string", "null"] },
        "air_date": {
          "type": ["string", "null"],
          "format": "date-time",
          "description": "Original air date ISO 8601"
        },
        "record_date": {
          "type": "string",
          "format": "date-time",
          "description": "When this recording was made"
        },
        "duration": {
          "type": "number",
          "description": "Duration in seconds"
        },
        "rating": { "type": ["string", "null"] },
        "description": { "type": ["string", "null"] },
        "actors": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["name"],
            "properties": {
              "name": { "type": "string" },
              "role": { "type": ["string", "null"] },
              "billing_order": { "type": ["integer", "null"] }
            }
          }
        }
      }
    },
    "transcript": {
      "type": "object",
      "description": "Full transcript data",
      "required": ["raw_text", "word_count", "language"],
      "properties": {
        "raw_text": {
          "type": "string",
          "description": "Complete transcript as plain text"
        },
        "cleaned_text": {
          "type": ["string", "null"],
          "description": "Cleaned transcript (filler words removed, punctuation normalized)"
        },
        "word_count": { "type": "integer" },
        "language": {
          "type": "string",
          "description": "Detected language code (e.g. 'en')"
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "description": "Average transcription confidence"
        },
        "model": {
          "type": "string",
          "description": "Whisper model used (e.g. 'large-v3', 'medium')"
        },
        "transcribed_at": {
          "type": "string",
          "format": "date-time",
          "description": "When transcription completed"
        }
      }
    },
    "chunks": {
      "type": "array",
      "description": "Transcript split into 30-second windows",
      "items": {
        "type": "object",
        "required": ["index", "start_time", "end_time", "text"],
        "properties": {
          "index": {
            "type": "integer",
            "description": "0-based chunk index"
          },
          "start_time": {
            "type": "number",
            "description": "Start time in seconds"
          },
          "end_time": {
            "type": "number",
            "description": "End time in seconds"
          },
          "text": {
            "type": "string",
            "description": "Transcript text for this chunk"
          },
          "speaker": {
            "type": ["string", "null"],
            "description": "Speaker label if diarization available"
          },
          "confidence": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 1,
            "description": "Average confidence for this chunk"
          },
          "word_count": { "type": "integer" }
        }
      }
    },
    "summary": {
      "type": ["object", "null"],
      "description": "LLM-generated summary (populated after enrichment)",
      "properties": {
        "text": {
          "type": "string",
          "description": "Natural-language summary of the recording"
        },
        "keywords": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Extracted keywords"
        },
        "topics": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Extracted topics"
        },
        "generated_at": {
          "type": "string",
          "format": "date-time"
        }
      }
    }
  }
}

Y.4 Example Sidecar

{
  "version": "1.0",
  "recording_id": "sagetv-12345",
  "system": "sagetv",
  "metadata": {
    "title": "Breaking Bad",
    "episode_title": "Bit by a Dead Bee",
    "season": 2,
    "episode": 3,
    "genre": ["Drama", "Crime", "Thriller"],
    "channel": "AMC",
    "channel_number": "245",
    "air_date": "2009-03-22T21:00:00Z",
    "record_date": "2025-01-15T21:00:00Z",
    "duration": 2820.5,
    "rating": "TV-MA",
    "description": "Walt and Jesse deal with the aftermath...",
    "actors": [
      { "name": "Bryan Cranston", "role": "Walter White", "billing_order": 1 },
      { "name": "Aaron Paul", "role": "Jesse Pinkman", "billing_order": 2 }
    ]
  },
  "transcript": {
    "raw_text": "Previously on Breaking Bad...",
    "cleaned_text": "Previously on Breaking Bad...",
    "word_count": 4250,
    "language": "en",
    "confidence": 0.94,
    "model": "large-v3",
    "transcribed_at": "2025-01-15T23:15:00Z"
  },
  "chunks": [
    {
      "index": 0,
      "start_time": 0.0,
      "end_time": 30.0,
      "text": "Previously on Breaking Bad. You got one part of that wrong.",
      "speaker": null,
      "confidence": 0.96,
      "word_count": 10
    },
    {
      "index": 1,
      "start_time": 30.0,
      "end_time": 60.0,
      "text": "This is not meth. This is something else entirely.",
      "speaker": null,
      "confidence": 0.93,
      "word_count": 9
    }
  ],
  "summary": {
    "text": "Walt and Jesse navigate the fallout from Tuco's death...",
    "keywords": ["Walter White", "Jesse", "DEA", "alibi", "hospital"],
    "topics": ["aftermath", "deception", "family tension"],
    "generated_at": "2025-01-15T23:20:00Z"
  }
}

Y.5 Notes
* Maximum sidecar file size target: < 5MB per recording
* Chunks should not exceed 500 words each
* The summary field is optional and populated asynchronously by the LLM
* Sidecar files are the source of truth for re-indexing (if the SQLite DB is lost,
  re-index by scanning all sidecar files)

End of Appendix Y
