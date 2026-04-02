=========================================
Appendix F — HTML5 Remote UI Specification
=========================================
This appendix defines the complete UI/UX specification for the HTML5 Remote used to control SageTV and ChannelsDVR through the LLM Gateway and Unified Session Manager.
It is written explicitly for GitHub Copilot to generate:
* The full HTML5 UI
* The UI state machine
* The device picker
* The system picker
* The voice input flow
* The LLM response bubble UI
* The admin panel
* The session binding UI
* The error states
* The mobile/desktop layout
This is the authoritative UI specification.

F.1 Design Principles
The HTML5 Remote must be:
? Fast
Minimal latency, instant UI response.
? Simple
One screen for 95% of interactions.
? Voicefirst
Buttonpress voice input is the primary interaction.
? Deterministic
User always knows which device/system they are controlling.
? Multiroom aware
Device picker is always visible.
? LLMassisted
Naturallanguage commands are supported.
? Localonly
No cloud dependencies.

F.2 HighLevel UI Layout
The UI is divided into five primary regions:
+-----------------------------------------------------------+
| 1. Header: System Picker + Device Picker                  |
+-----------------------------------------------------------+
| 2. Now Playing Panel                                      |
+-----------------------------------------------------------+
| 3. Transport Controls (Play/Pause/Skip/Seek/Stop)         |
+-----------------------------------------------------------+
| 4. Voice Input + Text Input + LLM Response Bubbles        |
+-----------------------------------------------------------+
| 5. Footer: Settings + Admin Panel                         |
+-----------------------------------------------------------+

F.3 Region 1 — Header
F.3.1 System Picker
Dropdown with:
* SageTV
* ChannelsDVR
Behavior:
* Selecting a system switches the entire backend context
* Device list updates to show only devices for that system
* Session resolution refreshes

F.3.2 Device Picker
Dropdown showing:
* Friendly device names
* Platform icons
* Lastseen status
Behavior:
* Selecting a device binds the remote to that device
* Unified Session Manager resolves the active playback session
* Now Playing panel updates
* Transport controls update
Device Status Indicators:
* Green dot = active
* Yellow dot = stale
* Red dot = offline

F.4 Region 2 — Now Playing Panel
Displays:
* Title
* Episode
* Channel
* Thumbnail (if available)
* Playback position
* Duration
* State (playing, paused, stopped)
Requirements:
* Must update every 2 seconds
* Must show “No active playback” if session is idle
* Must show “Resolving session…” during session lookup

F.5 Region 3 — Transport Controls
Buttons:
* Play / Pause
* Stop
* Skip Forward (30s)
* Skip Back (10s)
* Seek slider
* Volume slider
* Mute toggle
* Commercial Skip (ChannelsDVR only)
* Playback Speed (ChannelsDVR only)
Behavior:
* All buttons call LLM Gateway ? MCP tool
* Disabled if no active session
* Disabled if device does not support capability

F.6 Region 4 — Voice + Text Input + LLM Responses
F.6.1 Voice Input Button
A large circular button:
* Press and hold = record
* Release = send audio to STT
* STT ? LLM Gateway ? MCP
States:
* Idle
* Recording
* Processing
* Error

F.6.2 Text Input Field
Allows typed naturallanguage commands.
Examples:
* “Skip ahead 2 minutes”
* “Record the next episode of Jeopardy”
* “What’s playing in the living room?”

F.6.3 LLM Response Bubbles
Chatstyle bubbles showing:
* Naturallanguage response
* Structured action summary
* Confirmation prompts (for CONFIRM actions)
Example:
LLM: Skipping ahead 30 seconds.
Confirmation Example:
LLM: Are you sure you want to delete this recording?
[Yes] [No]

F.7 Region 5 — Footer
Contains:
* Settings
* Admin Panel
* About

F.8 Settings Panel
Settings include:
1. STT Settings
* Model selection (auto/manual)
* Microphone selection
* Sensitivity
2. LLM Settings
* Model selection (Ollama models)
* Temperature
* Max tokens
3. Device Settings
* Default device
* Device limit
4. System Settings
* Default system (SageTV or ChannelsDVR)

F.9 Admin Panel
Admin panel includes:
1. Device Administration
* List devices
* Rename device
* Delete device
* View capabilities
* View last seen
* Set default device
2. System Administration
* Restart SageTV service
* Restart ChannelsDVR service
* Clear caches
* Rebuild indexes
3. Transcription Subsystem
* View transcription jobs
* View errors
* Retry failed jobs

F.10 Error States
1. No Device Selected
Show:
Please select a device to begin.
2. No Active Session
Show:
No active playback on this device.
3. Session Resolution Failed
Show:
Unable to resolve playback session. Please try again.
4. MCP Server Offline
Show:
SageTV/ChannelsDVR server is unreachable.
5. LLM Error
Show:
I didn’t understand that. Try again.

F.11 Mobile Layout
Requirements:
* Singlecolumn layout
* Sticky voice button
* Collapsible Now Playing panel
* Swipe gestures for skip/seek

F.12 Desktop Layout
Requirements:
* Multicolumn layout
* Larger Now Playing panel
* Full transport control row
* Sidebar for admin panel

F.13 Accessibility Requirements
* Keyboard navigation
* Screen reader support
* Highcontrast mode
* Large text mode
