=========================================
Appendix C — MCP Architecture (ThreeServer Model)
=========================================
This appendix defines the complete, authoritative architecture for the threeserver MCP system used by the LLM Remote Control platform.
It is written explicitly for GitHub Copilot to generate:
* The SageTV MCP server
* The ChannelsDVR MCP server
* The Linux MCP server
* The Orchestrator (bridge between LLM and MCP servers)
* Tool routing logic
* Resource models
* Safety enforcement
* Error handling
* Namespacing rules
This is the core integration layer of the entire system.

C.1 MCP Overview
The Model Context Protocol (MCP) is the standard interface between:
* The LLM
* External tools
* External data sources
* External services
In this system, MCP is used to expose:
* SageTV capabilities
* ChannelsDVR capabilities
* Linux system capabilities
Each MCP server is:
* Independent
* Namespaced
* Secure
* Capabilityscoped
* Replaceable
* Discoverable
The orchestrator merges all three into a unified tool registry for the LLM.

C.2 ThreeServer Architecture
The system uses three independent MCP servers:
1. SageTV MCP Server
Purpose:
Expose SageTV’s media control, DVR, EPG, and system capabilities.
Implements:
* SageTV Capability Dictionary (Appendix A)
* SageTV tool registry
* SageTV resource model
* Session resolution helpers

2. ChannelsDVR MCP Server
Purpose:
Expose ChannelsDVR’s playback, DVR, EPG, commercial skip, and system capabilities.
Implements:
* ChannelsDVR Capability Dictionary (Appendix B)
* ChannelsDVR tool registry
* ChannelsDVR resource model
* Session resolution helpers

3. Linux MCP Server
Purpose:
Expose safe, allowlisted systemlevel operations.
Capabilities include:
* Service status
* Service restart (allowlisted)
* Disk usage
* Network info
* Log viewing (allowlisted paths)
* Docker container management (allowlisted)
This server is privileged and must enforce strict allowlists.

C.3 Why Three Servers?
? Failure Isolation
A crash in Linux MCP cannot affect SageTV or ChannelsDVR control.
? Security Boundaries
SageTV and ChannelsDVR are networkonly.
Linux MCP is privileged.
They must not share a process.
? Independent Versioning
Each server can be updated independently.
? Reusability
Linux MCP can be reused for other projects.
? Clean Separation of Concerns
Each server exposes only its own capability dictionary.

C.4 MCP Server Responsibilities
Each MCP server must implement:
1. Tool Registry
* List of callable functions
* Input schema
* Output schema
* Safety level
2. Resource Registry
* Readonly data sources
* Cache TTL
* Query parameters
3. Prompt Registry (Optional)
Reusable workflows (e.g., “find and record”).
4. Safety Enforcement
* SAFE
* CONFIRM
* DANGEROUS
* OWNER
5. Error Handling
* Invalid parameters
* Missing entities
* Backend errors
* Timeouts
6. Logging
* Tool calls
* Errors
* Confirmations
* Ownerlevel actions

C.5 Orchestrator Architecture
The orchestrator is the bridge between:
* The LLM (Ollama or llama.cpp)
* The three MCP servers
It performs:
C.5.1 Tool Discovery
On startup:
1. Connect to each MCP server
2. Call list_tools()
3. Convert MCP tool schemas ? OpenAI function definitions
4. Prefix tool names: 
o sagetv_
o channels_
o linux_
5. Merge into a unified registry

C.5.2 Resource Discovery
Same process for resources:
* sagetv://…
* channels://…
* linux://…

C.5.3 Context Injection
Before sending a prompt to the LLM:
* Query the Unified Session Manager
* Query MCP resources for: 
o Now playing
o Device info
o Playback session
o Media metadata
* Inject into the system prompt

C.5.4 Tool Routing
When the LLM returns a tool call:
1. Parse the tool name
2. Determine namespace
3. Route to correct MCP server
4. Execute via call_tool()
5. Append result to message history
6. Continue LLM loop

C.5.5 MultiStep Execution
The orchestrator supports:
* Multitool workflows
* Iterative refinement
* Error recovery
* Confirmation prompts

C.6 Namespacing Rules
All tools must be prefixed:
* sagetv_play_media
* channels_pause_playback
* linux_service_status
This prevents collisions and ensures deterministic routing.

C.7 Safety Enforcement
Safety is enforced at three layers:
1. LLM Prompt Layer
The LLM is instructed to:
* Use only allowed tools
* Request confirmation for CONFIRM actions
* Refuse disallowed actions
2. Orchestrator Layer
The orchestrator:
* Validates tool parameters
* Blocks disallowed actions
* Enforces confirmation
* Enforces owner authentication
3. MCP Server Layer
Each MCP server:
* Enforces capability dictionary
* Enforces allowlists (Linux MCP)
* Rejects invalid or unsafe calls

C.8 Error Handling Model
Errors must be:
* Structured
* Humanreadable
* Logged
* Returned to the LLM for recovery
Example:
{
  "error": "invalid_airing_id",
  "message": "The airing ID 12345 does not exist.",
  "suggestions": ["List upcoming recordings", "Search for the show again"]
}

C.9 MCP Server Implementation Requirements
Each server must:
* Use JSONRPC 2.0
* Support streaming HTTP
* Support list_tools, call_tool, list_resources, read_resource
* Support structured schemas
* Support logging
* Support authentication (Linux MCP owner commands)

C.10 Orchestrator Implementation Requirements
The orchestrator must:
* Use OpenAIcompatible tool calling
* Support multistep workflows
* Support context injection
* Support session resolution
* Support safety enforcement
* Support error recovery
* Support logging

