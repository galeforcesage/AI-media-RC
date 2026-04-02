=========================================
Appendix G — MCP Tool Definitions
=========================================
This appendix defines the complete MCP tool registry for:
1. SageTV MCP Server
2. ChannelsDVR MCP Server
3. Linux MCP Server
It also defines:
* Tool naming conventions
* Input/output schemas
* Safety levels
* Error handling
* Sessionaware routing
* Orchestrator integration
This is the authoritative specification for GitHub Copilot to implement the MCP layer.

G.1 MCP Tool Naming Convention
All tools must be namespaced:
* sagetv_*
* channels_*
* linux_*
This ensures:
* No collisions
* Deterministic routing
* Clear system boundaries
Examples:
* sagetv_pause_playback
* channels_seek_relative
* linux_restart_service

G.2 Tool Schema Format
Each tool must define:
{
  "name": "sagetv_pause_playback",
  "description": "Pause playback on the active SageTV session.",
  "input_schema": {
      "type": "object",
      "properties": {
          "session_id": { "type": "string" }
      },
      "required": ["session_id"]
  },
  "output_schema": {
      "type": "object",
      "properties": {
          "success": { "type": "boolean" },
          "message": { "type": "string" }
      }
  },
  "safety": "SAFE"
}

G.3 Safety Levels
SAFE
* No confirmation needed
* Reversible
* Playback control, queries
CONFIRM
* Requires user confirmation
* Cancel recording, remove favorite
DANGEROUS
* Destructive
* Delete recording file
OWNER
* Requires authenticated owner session
* Restart services, rebuild indexes

G.4 SageTV MCP Tool Definitions
Below is the complete SageTV tool registry, aligned with Appendix A.

G.4.1 Playback Tools
sagetv_pause_playback
* API: MediaPlayerAPI.Pause
* Input: 
o session_id: string
* Safety: SAFE
sagetv_resume_playback
* API: MediaPlayerAPI.Play
* Input: 
o session_id
* Safety: SAFE
sagetv_stop_playback
* API: MediaPlayerAPI.Stop
* Input: 
o session_id
* Safety: SAFE
sagetv_skip_forward
* API: MediaPlayerAPI.SkipForward
* Input: 
o session_id
* Safety: SAFE
sagetv_skip_back
* API: MediaPlayerAPI.SkipBack
* Input: 
o session_id
* Safety: SAFE
sagetv_seek_relative
* API: MediaPlayerAPI.SeekRelative
* Input: 
o session_id
o seconds: integer
* Safety: SAFE
sagetv_seek_absolute
* API: MediaPlayerAPI.Seek
* Input: 
o session_id
o position_seconds: integer
* Safety: SAFE
sagetv_tune_channel
* API: MediaPlayerAPI.WatchLive
* Input: 
o session_id
o channel: string
* Safety: SAFE

G.4.2 Recording Tools
sagetv_record_show
* API: AiringAPI.Record
* Input: 
o airing_id: string
* Safety: SAFE
sagetv_cancel_recording
* API: AiringAPI.CancelRecord
* Input: 
o airing_id: string
* Safety: CONFIRM
sagetv_delete_media_file
* API: MediaFileAPI.DeleteFile
* Input: 
o media_file_id: string
* Safety: DANGEROUS

G.4.3 Favorites Tools
sagetv_create_favorite
* API: FavoriteAPI.AddFavorite
* Input: 
o title: string
o channel: string | null
* Safety: SAFE
sagetv_remove_favorite
* API: FavoriteAPI.RemoveFavorite
* Input: 
o favorite_id: string
* Safety: CONFIRM

G.4.4 System Tools
sagetv_run_library_scan
* API: Global.RunLibraryImportScan
* Input: none
* Safety: OWNER
sagetv_restart_service
* API: Linux MCP ? systemctl restart sagetv
* Input: none
* Safety: OWNER

G.5 ChannelsDVR MCP Tool Definitions
Aligned with Appendix B.

G.5.1 Playback Tools
channels_pause_playback
* API: /sessions/{session_id}/pause
* Input: 
o session_id
* Safety: SAFE
channels_resume_playback
* API: /sessions/{session_id}/play
* Input: 
o session_id
* Safety: SAFE
channels_stop_playback
* API: /sessions/{session_id}/stop
* Input: 
o session_id
* Safety: SAFE
channels_seek_relative
* API: /sessions/{session_id}/seek?offset={seconds}
* Input: 
o session_id
o seconds: integer
* Safety: SAFE
channels_seek_absolute
* API: /sessions/{session_id}/seek?position={seconds}
* Input: 
o session_id
o position_seconds: integer
* Safety: SAFE
channels_skip_commercial
* API: /sessions/{session_id}/commercial/next
* Input: 
o session_id
* Safety: SAFE
channels_previous_commercial
* API: /sessions/{session_id}/commercial/prev
* Input: 
o session_id
* Safety: SAFE
channels_set_playback_speed
* API: /sessions/{session_id}/speed?rate={rate}
* Input: 
o session_id
o rate: float
* Safety: SAFE

G.5.2 Recording Tools
channels_schedule_recording
* API: /dvr/rules
* Input: 
o program_id
o channel
o start_time
o end_time
* Safety: SAFE
channels_schedule_series_recording
* API: /dvr/rules
* Input: 
o series_id
o channel
o options
* Safety: SAFE
channels_cancel_scheduled_recording
* API: /dvr/rules/{id}
* Input: 
o id
* Safety: CONFIRM
channels_delete_recording
* API: /dvr/files/{id}
* Input: 
o id
* Safety: CONFIRM
channels_delete_recording_file
* API: /dvr/files/{id}?delete=true
* Input: 
o id
* Safety: DANGEROUS

G.5.3 Commercial Tools
channels_regenerate_commercial_markers
* API: /dvr/files/{id}/commercials/rebuild
* Input: 
o id
* Safety: CONFIRM

G.5.4 System Tools
channels_restart_service
* API: Linux MCP ? systemctl restart channels-dvr
* Input: none
* Safety: OWNER
channels_clear_cache
* API: /dvr/cache/clear
* Input: none
* Safety: OWNER
channels_rebuild_index
* API: /dvr/index/rebuild
* Input: none
* Safety: OWNER

G.6 Linux MCP Tool Definitions
These tools are privileged and must be strictly allowlisted.

G.6.1 Service Tools
linux_restart_service
* Input: 
o service_name: string
o Must be in allowlist: 
* sagetv
* channels-dvr
* docker
* Safety: OWNER
linux_service_status
* Input: 
o service_name
* Safety: SAFE

G.6.2 System Tools
linux_disk_usage
* Input: none
* Safety: SAFE
linux_network_info
* Input: none
* Safety: SAFE
linux_tail_log
* Input: 
o path: string (must be allowlisted)
o lines: integer
* Safety: OWNER

G.7 Orchestrator Tool Routing Logic
When LLM returns a tool call:
1. Parse tool name
2. Determine namespace
3. Route to correct MCP server
4. Validate input schema
5. Enforce safety level
6. Execute tool
7. Return structured result
8. Continue LLM loop

G.8 Error Handling Schema
All tools must return:
{
  "success": false,
  "error": "string",
  "message": "human readable explanation",
  "suggestions": ["optional", "list"]
}

