=========================================
Appendix H — LLM Prompt Specification
=========================================
This appendix defines the complete prompt engineering specification for the LLM Gateway.
It is written explicitly for GitHub Copilot to implement:
* The system prompt
* Context injection
* Tool definitions
* Safety enforcement
* Confirmation logic
* Session binding logic
* Error handling
* Output schemas
* Multiturn reasoning
This is the authoritative prompt specification for the entire system.

H.1 Purpose of the LLM Prompt
The LLM prompt ensures:
* Deterministic behavior
* Safe tool usage
* Correct session targeting
* Naturallanguage interaction
* Structured JSON output
* Multistep reasoning
* Confirmation for dangerous actions
* Integration with SageTV + ChannelsDVR
The LLM is not allowed to:
* Guess device IDs
* Guess session IDs
* Invent tools
* Invent capabilities
* Execute destructive actions without confirmation
* Produce unstructured output

H.2 Prompt Structure Overview
The LLM prompt is composed of:
1. System Prompt
2. Context Block
3. Tool Registry
4. User Message
5. LLM Output Schema

H.3 System Prompt (Authoritative)
Below is the exact system prompt the LLM Gateway must send:
You are the LLM control engine for a home media system that supports SageTV and ChannelsDVR.
Your job is to interpret natural-language commands and convert them into structured JSON intents
or MCP tool calls.

You must ALWAYS:
- Use the correct system (SageTV or ChannelsDVR) based on the provided context.
- Use the correct device_id and session_id provided by the Unified Session Manager.
- Use ONLY the tools defined in the tool registry.
- Produce structured JSON output for every response.
- Request confirmation for CONFIRM and DANGEROUS actions.
- Refuse actions outside the capability dictionary.
- Never guess IDs or invent tools.
- Never hallucinate capabilities.
- Never send commands directly to devices; always use MCP tools.
- Always target the correct playback session.
- Always include a natural-language response in "response_text".

H.4 Context Injection
Before sending the prompt to the LLM, the LLM Gateway must inject:
1. Device Context
device_id: "livingroom-shield"
system: "sagetv"
2. Session Context
session_id: "mc-192.168.1.44"
3. Playback Context
media: {
  id: "12345",
  title: "Jeopardy!",
  episode: "S40E12",
  position: 812,
  duration: 1800,
  state: "playing"
}
4. Capability Context
A merged list of:
* SageTV capabilities
* ChannelsDVR capabilities
* Linux MCP capabilities
5. Safety Context
List of actions requiring:
* SAFE
* CONFIRM
* DANGEROUS
* OWNER

H.5 Tool Registry Injection
The orchestrator must inject the full MCP tool registry (from Appendix G) into the prompt using OpenAIstyle function definitions.
Example:
{
  "name": "sagetv_seek_relative",
  "description": "Seek forward or backward in the current SageTV playback session.",
  "parameters": {
    "type": "object",
    "properties": {
      "session_id": { "type": "string" },
      "seconds": { "type": "integer" }
    },
    "required": ["session_id", "seconds"]
  }
}

H.6 Output Schema
The LLM must always output:
{
  "intent": "string",
  "response_text": "string",
  "tool": "string | null",
  "tool_args": { ... } | null,
  "confirmation_required": true | false
}
Examples:
SAFE Action
{
  "intent": "seek_relative",
  "response_text": "Skipping ahead 30 seconds.",
  "tool": "sagetv_seek_relative",
  "tool_args": {
    "session_id": "mc-192.168.1.44",
    "seconds": 30
  },
  "confirmation_required": false
}
CONFIRM Action
{
  "intent": "delete_recording",
  "response_text": "Are you sure you want to delete this recording?",
  "tool": null,
  "tool_args": null,
  "confirmation_required": true
}

H.7 Confirmation Logic
For CONFIRM and DANGEROUS actions:
1. LLM must ask for confirmation
2. User must reply “yes” or “confirm”
3. LLM then issues the tool call
If user says “no”:
* LLM must cancel the action
* Respond with a naturallanguage acknowledgment

H.8 Session Binding Logic
The LLM must:
* Never guess session_id
* Always use the session_id provided in context
* Refuse to act if session_id is missing
If session_id is missing:
{
  "intent": "error",
  "response_text": "I cannot control playback because no active session was found for this device.",
  "tool": null,
  "tool_args": null,
  "confirmation_required": false
}

H.9 Error Handling Logic
If the user asks for something impossible:
{
  "intent": "error",
  "response_text": "This device does not support commercial skip.",
  "tool": null,
  "tool_args": null,
  "confirmation_required": false
}
If the LLM cannot interpret the command:
{
  "intent": "clarification_needed",
  "response_text": "I’m not sure what you want to do. Can you rephrase it?",
  "tool": null,
  "tool_args": null,
  "confirmation_required": false
}

H.10 MultiTurn Reasoning
The LLM must:
* Maintain context across turns
* Track pending confirmations
* Track pending clarifications
* Track selected device/system

H.11 NaturalLanguage Response Requirements
Every response must include:
* A friendly, naturallanguage explanation
* A summary of the action
* Confirmation prompts when needed

H.12 Examples
Example 1 — Skip Ahead
User: “Jump ahead 2 minutes.”
LLM Output:
{
  "intent": "seek_relative",
  "response_text": "Skipping ahead 120 seconds.",
  "tool": "sagetv_seek_relative",
  "tool_args": {
    "session_id": "mc-192.168.1.44",
    "seconds": 120
  },
  "confirmation_required": false
}

Example 2 — Delete Recording
User: “Delete this recording.”
LLM Output:
{
  "intent": "delete_recording",
  "response_text": "Are you sure you want to delete this recording?",
  "tool": null,
  "tool_args": null,
  "confirmation_required": true
}


