=========================================
Appendix D — Transcription Subsystem Specification
=========================================
This appendix defines the complete, standalone transcription subsystem used by both SageTV and ChannelsDVR.
It is designed for:
* Highaccuracy transcription
* Zero cloud dependency
* SSDbased extraction
* Queuebased processing
* Integration with the LLM Remote
* Integration with both MCP servers
* Multisystem support (SageTV + ChannelsDVR)
This subsystem is independent of the LLM Remote but integrates with it through MCP resources and metadata.

D.1 Purpose
The transcription subsystem provides:
* Automatic transcription of newly created recordings
* Highaccuracy Whisper Largev3 transcription
* SSDbased temporary extraction to avoid HDD contention
* Metadata output for search, summarization, and LLM context
* Integration with SageTV and ChannelsDVR
* A unified API for the LLM Remote
This enables:
* Naturallanguage search of recordings
* Episode summaries
* Scene detection
* Quote search
* Topic extraction
* Voicedriven “find the episode where…” queries

D.2 Architecture Overview
[Recording Created]
   ?
[File Watcher]
   ?
[Extraction Worker]
   ?
[SSD Temp Storage]
   ?
[Transcription Queue]
   ?
[Whisper Largev3 Engine]
   ?
[Transcript + Metadata]
   ?
[Metadata Store]
   ?
[MCP Resource Exposure]
   ?
[LLM Remote / Search / Summaries]

D.3 Subsystem Components
1. File Watcher
Two watchers run independently:
* sagetv_watcher
* channels_watcher
Each watcher:
* Monitors a configured directory
* Detects new files
* Detects fileclosed events (recording finished)
* Sends jobs to the queue
Requirements:
* Must ignore partial files
* Must handle file renames
* Must handle multipart recordings
* Must debounce rapid events

2. Extraction Worker
Purpose:
* Extract audio from the recording
* Write to SSD temp directory
* Avoid HDD contention
Requirements:
* Use ffmpeg or ffprobe to detect audio stream
* Extract to .wav or .flac
* Use SSD path (configurable)
* Delete temp files after transcription

3. Transcription Queue
A persistent queue that:
* Stores jobs
* Supports retries
* Supports prioritization
* Supports concurrency limits
Job schema:
{
  "job_id": "uuid",
  "system": "sagetv" | "channelsdvr",
  "recording_id": "string",
  "file_path": "string",
  "temp_audio_path": "string",
  "status": "pending|processing|done|error",
  "attempts": 0
}

4. Whisper Engine
The subsystem uses:
* Whisper Largev3 for full transcription
* Whisper Medium/Small if system RAM is insufficient
* Automatic model selection (Appendix A logic)
Requirements:
* Use fasterwhisper (CTranslate2)
* Support GPU if available
* Support CPU fallback
* Support chunked transcription for long files
* Output timestamps

5. Metadata Generator
After transcription:
* Generate a .json metadata file
* Generate a .txt transcript
* Generate a .vtt subtitle file
* Generate a .summary file (LLMgenerated)
Metadata includes:
* Title
* Episode
* Duration
* Word count
* Speaker segmentation (optional)
* Topic list
* Keywords
* Summary
* Scene boundaries

6. Metadata Store
Stores:
* Transcripts
* Summaries
* Metadata
* Search index
Requirements:
* Use SQLite or lightweight embedded DB
* Store perrecording metadata
* Support fulltext search (FTS5 recommended)

7. MCP Resource Exposure
The subsystem exposes resources to the LLM via:
transcript://{system}/{recording_id}
Returns:
{
  "transcript": "...",
  "summary": "...",
  "keywords": [...],
  "topics": [...],
  "scenes": [...]
}
transcript://search/{query}
Returns ranked results.

D.4 Integration with SageTV
When SageTV finishes a recording:
* File watcher detects file close
* Job is queued
* Transcript is generated
* Metadata is stored
* MCP resource exposes transcript
* LLM Remote can: 
o Summarize
o Search
o Answer questions about the episode

D.5 Integration with ChannelsDVR
ChannelsDVR provides:
* Recording metadata
* Commercial markers
* Job status
The subsystem:
* Watches ChannelsDVR recording directory
* Generates transcripts
* Stores metadata
* Exposes via MCP
LLM Remote can:
* Search recordings
* Summarize episodes
* Provide “find the scene where…” queries

D.6 Performance Requirements
* Must not interfere with recording
* Must not saturate HDD
* Must use SSD for temp
* Must support concurrency limit (default 1)
* Must support GPU acceleration if available

D.7 Error Handling
Errors must be:
* Logged
* Retried (max 3 attempts)
* Marked as failed
* Visible via MCP resource:
transcript://jobs

D.8 Configuration
Required:
* SageTV recording directory
* ChannelsDVR recording directory
* SSD temp directory
* Whisper model selection
* Concurrency limit
Optional:
* GPU enable
* Summary generation enable
* Scene detection enable

D.9 Security Model
* No external network calls
* No cloud LLM
* No cloud STT
* Localonly processing
* Metadata stored locally
* MCP resources readonly

