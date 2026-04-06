=========================================
Appendix A � SageTV Capability Dictionary
=========================================
This appendix defines the complete, authoritative capability dictionary for SageTV as exposed to the LLM through the SageTV MCP Server. It is the contract between:
* The LLM
* The MCP server
* The orchestrator
* The HTML5 remote
* The Unified Session Manager
GitHub Copilot will use this dictionary to generate:
* MCP tool definitions
* API wrappers
* Validation logic
* Safety boundaries
* Structured intent schemas
This dictionary is exhaustive, deterministic, and LLMsafe.
A.1 Overview
The SageTV Capability Dictionary defines:
1. Entities � objects the LLM can reference
2. State � readonly information
3. Actions � commands the LLM may invoke
4. Resources � MCPexposed read endpoints
5. Tools � MCPexposed write endpoints
6. Safety Levels � SAFE, CONFIRM, DANGEROUS, OWNER
7. Session Model � how playback sessions are resolved
8. Playback Context Model � what the LLM sees about �what�s playing�
This dictionary is the only surface the LLM is allowed to use.
A.2 Entity Model
A.2.1 Media Entities
Entity
Description
Fields
Recording
A recorded TV airing
id, title, episode, season, channel, start_time, end_time, watched, file_path
Airing
A scheduled or upcoming airing
id, title, channel, start_time, end_time, is_recording
MediaFile
Any media file (recording or imported video)
id, type, title, metadata, duration, watched
Favorite
A series recording rule
id, title, channel, padding, quality, enabled
Show
EPG show metadata
id, title, description, actors, year, rating
Channel
A TV channel
id, number, name, lineup
Playlist
A user playlist
id, name, items
A.2.2 Playback Entities
Entity
Description
Fields
PlaybackSession
Active playback instance
session_id, device_id, media_file_id, position, duration, state
Device
A playback device
device_id, name, ip, type, capabilities
A.2.3 System Entities
Entity
Description
Fields
Tuner
Capture device
id, name, status
Disk
Storage volume
path, free_space, total_space
Client
Connected SageTV client
id, ip, type, last_seen
A.3 ReadOnly State (LLMVisible)
The LLM may read:
* Now playing media
* Playback position
* Playback duration
* Playback state (playing, paused, stopped)
* Upcoming recordings
* Recent recordings
* Scheduled recordings
* Favorites
* Channels
* EPG search results
* Disk space
* Tuner status
* Active clients
* Device registry entries
The LLM may not read:
* File system paths outside SageTV
* Credentials
* Network configuration
* OSlevel information
A.4 Actions (LLMCallable)
Actions are grouped by safety level.
A.4.1 SAFE Actions (No Confirmation Required)
Playback
* pause_playback
* resume_playback
* stop_playback
* skip_forward
* skip_back
* seek_relative
* seek_absolute
* set_volume
* mute
* unmute
* tune_channel
* open_recordings
* open_guide
* open_home
* open_live_tv
Queries
* get_now_playing
* get_recordings
* get_upcoming_recordings
* get_channels
* search_shows
* get_disk_space
* get_tuner_status
* get_clients
Favorites
* create_favorite
A.4.2 CONFIRM Actions (User Confirmation Required)
Recording Management
* cancel_recording
* remove_favorite
Configuration
* set_config_value
A.4.3 DANGEROUS Actions (Explicit Confirmation Required)
Destructive
* delete_media_file
These actions must:
* Be confirmed by the user
* Be logged
* Include a safety explanation
A.4.4 OWNER Actions (Authentication Required)
Administrative
* restart_sagetv_service
* restart_plugin
* clear_cache
* run_library_scan
These require:
* �server owner wants you to:� prefix
* Authentication
* Elevated session (15 minutes)
A.5 MCP Resources (Read Endpoints)
Resource URI
Description
Backing API
sagetv://media/recordings
All recordings
MediaFileAPI.GetMediaFiles("T")
sagetv://media/videos
Imported videos
MediaFileAPI.GetMediaFiles("V")
sagetv://media/now-playing
Current playback
MediaPlayerAPI.GetCurrentMediaFile
sagetv://media/{id}
Media file details
MediaFileAPI.GetMediaFileForID
sagetv://channels
All channels
ChannelAPI.GetAllChannels
sagetv://epg/search/{query}
EPG search
ShowAPI.SearchShowsByTitle
sagetv://recordings/scheduled
Scheduled recordings
AiringAPI.GetScheduledRecordings
sagetv://favorites
Favorites
FavoriteAPI.GetFavorites
sagetv://system/status
Disk + tuner + clients
Multiple APIs
A.6 MCP Tools (Write Endpoints)
Below is the complete tool registry for SageTV.
Each tool includes:
* Name
* Parameters
* Backing API
* Safety level
* Return schema
A.6.1 Playback Tools
pause_playback
* API: MediaPlayerAPI.Pause
* Params: none
* Safety: SAFE
resume_playback
* API: MediaPlayerAPI.Play
* Params: none
* Safety: SAFE
stop_playback
* API: MediaPlayerAPI.Stop
* Params: none
* Safety: SAFE
skip_forward
* API: MediaPlayerAPI.SkipForward
* Params: none
* Safety: SAFE
skip_back
* API: MediaPlayerAPI.SkipBack
* Params: none
* Safety: SAFE
seek_relative
* API: MediaPlayerAPI.SeekRelative
* Params: seconds: integer
* Safety: SAFE
seek_absolute
* API: MediaPlayerAPI.Seek
* Params: position_seconds: integer
* Safety: SAFE
set_volume
* API: MediaPlayerAPI.SetVolume
* Params: level: integer (0�100)
* Safety: SAFE
tune_channel
* API: MediaPlayerAPI.WatchLive
* Params: channel: string
* Safety: SAFE
A.6.2 Recording Tools
record_show
* API: AiringAPI.Record
* Params: airing_id: string
* Safety: SAFE
cancel_recording
* API: AiringAPI.CancelRecord
* Params: airing_id: string
* Safety: CONFIRM
delete_media_file
* API: MediaFileAPI.DeleteFile
* Params: media_file_id: string
* Safety: DANGEROUS
A.6.3 Favorites Tools
create_favorite
* API: FavoriteAPI.AddFavorite
* Params: title: string, channel: string | null
* Safety: SAFE
remove_favorite
* API: FavoriteAPI.RemoveFavorite
* Params: favorite_id: string
* Safety: CONFIRM
A.6.4 Configuration Tools
set_config_value
* API: Configuration.SetProperty
* Params: key: string, value: string
* Safety: CONFIRM
get_config_value
* API: Configuration.GetProperty
* Params: key: string
* Safety: SAFE
A.6.5 System Tools
run_library_scan
* API: Global.RunLibraryImportScan
* Params: none
* Safety: OWNER
restart_sagetv_service
* API: Linux MCP ? systemctl
* Params: none
* Safety: OWNER
A.7 Session Resolution Model
The Unified Session Manager resolves the active playback session by:
1. Querying SageTV MCP for active clients
2. Matching device_id to client_id
3. Querying MediaPlayerAPI for that client
4. Returning:
o session_id
o media_file_id
o position
o duration
o state
This ensures commands always target the correct playback session.
A.8 Playback Context Model
The LLM receives a structured context block:
Code
{
  "device_id": "livingroom-shield",
  "session_id": "mc-192.0.2.10",
  "media": {
    "id": "12345",
    "title": "Jeopardy!",
    "episode": "S40E12",
    "position": 812,
    "duration": 1800,
    "state": "playing"
  }
}
This allows the LLM to:
* Interpret commands
* Resolve ambiguity
* Provide natural responses

