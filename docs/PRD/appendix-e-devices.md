=========================================
Appendix E — Device Registry & Pairing Protocol
=========================================
This appendix defines the complete devicebinding architecture for the HTML5 LLM Remote system.
It is written explicitly for GitHub Copilot to implement:
* Device registration
* Device identification
* Device pairing
* Device metadata
* Device capabilities
* Device admin panel
* Device lifecycle
* Integration with the Unified Session Manager
* Integration with SageTV MCP
* Integration with ChannelsDVR MCP
This is the authoritative specification.

E.1 Purpose
The Device Registry provides:
* A userfriendly list of playback devices
* A binding target for the HTML5 remote
* A mapping between device_id ? playback session
* A persistent store of known devices
* A pairing mechanism for SageTV and ChannelsDVR
* A capability model for each device
* A device admin interface
This ensures:
* Commands always target the correct playback session
* The LLM always knows which device the user means
* Multiroom households work reliably

E.2 Device Registry Overview
The registry is a persistent database (SQLite recommended) containing:
device_id (string, primary key)
friendly_name (string)
system ("sagetv" | "channelsdvr")
ip_address (string)
platform ("shield" | "chromecast" | "miniclient" | "browser" | "androidtv" | "pc" | "pi" | ...)
capabilities (json)
last_seen (timestamp)
paired_at (timestamp)
pairing_method ("qr" | "api" | "manual")
is_default (boolean)

E.3 Device Limit
* Configurable
* Default: 15 devices
* Hard upper limit: 50 devices
If the limit is reached:
* New devices cannot be paired
* Admin panel must show a warning
* User must delete an old device

E.4 Device Identification Model
Each device receives a stable device_id:
{system}-{platform}-{uuid}
Examples:
* sagetv-shield-9f3a2c
* channels-chromecast-1b22e1
* sagetv-miniclient-192168144
* channels-browser-7c1d9a

E.5 Device Metadata
Each device stores:
1. Platform
* shield
* chromecast
* miniclient
* placeshifter
* androidtv
* browser
* pc
* pi
2. Capabilities
{
  "supports_seek": true,
  "supports_volume": true,
  "supports_commercial_skip": false,
  "supports_playback_speed": true
}
3. Network Info
* IP address
* Last seen timestamp
4. System
* sagetv
* channelsdvr

E.6 Device Pairing Protocol
There are three pairing methods:

E.6.1 SageTV QRCode Pairing (Primary Method)
Flow:
1. User opens SageTV UI
2. SageTV plugin displays a QR code
3. QR code contains:
sagetv://pair?device_id=...&ip=...&name=...
4. HTML5 remote scans QR code
5. Device is added to registry
6. Unified Session Manager can now resolve sessions for this device
Requirements:
* QR code must be shortlived (5 minutes)
* QR code must be signed (HMAC)
* Device_id must be unique

E.6.2 ChannelsDVR API Enumeration (Primary Method)
ChannelsDVR exposes connected clients via /clients.
Flow:
1. HTML5 remote calls ChannelsDVR MCP ? /clients
2. MCP returns list of clients
3. User selects a client
4. Device is added to registry
Requirements:
* Must handle clients appearing/disappearing
* Must store platform type (e.g., “Android TV”)

E.6.3 Manual Pairing (Fallback Method)
Flow:
1. User enters: 
o Device name
o IP address
o System (SageTV or ChannelsDVR)
2. System generates device_id
3. Device is added to registry
Use cases:
* Devices without QR scanning
* Devices not visible via API enumeration
* Headless devices

E.7 Device Admin Panel
The HTML5 remote must include a Device Administration UI.
Features:
* List all devices
* Rename device
* Delete device
* Set default device
* View device capabilities
* View last seen
* View platform
* View system
* Revoke pairing tokens
* Reset registry

E.8 Device Lifecycle
1. Registration
Device is added to registry.
2. Active
Device is selectable in the HTML5 remote.
3. Stale
If last_seen > 30 days, mark as stale.
4. Expired
If last_seen > 90 days, hide from default list.
5. Deleted
User removes device manually.

E.9 Integration with Unified Session Manager
When user selects a device:
1. HTML5 remote sends:
device_id
system
2. Unified Session Manager:
o Queries MCP server for active sessions
o Matches device_id ? client_id
o Retrieves session_id
o Returns playback context
3. LLM Gateway uses session_id for all commands.

E.10 Integration with SageTV MCP
SageTV MCP must expose:
* sagetv://clients
* sagetv://sessions
* sagetv://device/{device_id}
The MCP server must map:
* device_id ? client_id
* client_id ? session_id

E.11 Integration with ChannelsDVR MCP
ChannelsDVR MCP must expose:
* /clients
* /sessions
The MCP server must map:
* device_id ? client_id
* client_id ? session_id

E.12 Security Requirements
* QR codes must be signed
* Pairing tokens must expire
* Device registry must be local only
* No cloud pairing
* No external network calls
* Admin panel must require authentication

