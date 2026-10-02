"""OpenClaw HTTP runtime adapter.

Bridges the orchestrator to an OpenClaw Gateway over its OpenAI-compatible
``POST /v1/chat/completions`` endpoint.

Design
------
OpenClaw runs as a separate, hardened service (loopback-only, token auth). It
never touches the media systems directly. Instead, the orchestrator advertises
its own MCP-backed media tools as OpenAI *client* function tools. OpenClaw's
agent reasons and decomposes the task; whenever it wants media data it emits a
tool call, which this adapter executes **inside the orchestrator** by delegating
to the fallback AgentLoop's ``_execute_tool`` — inheriting all of its safety
rails (schema validation, confirmation gates for dangerous/owner tools,
temporal + system-scope guardrails). The loop continues until OpenClaw returns
a final assistant message or the plan-depth / timeout budget is exhausted.

This keeps tool execution and its guardrails on the orchestrator side while
OpenClaw acts purely as the multi-step planning brain.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)


class OpenClawHTTPRuntime:
    """Invoke an OpenClaw Gateway over HTTP with orchestrator-executed tools."""

    def __init__(
        self,
        orchestrator: Any,
        config: Dict[str, Any],
        fallback: Any,
    ) -> None:
        self._orch = orchestrator
        self._cfg = config or {}
        self._fallback = fallback  # AgentLoop; provides async _execute_tool

        self._base = str(self._cfg.get("gateway_url", "") or "").rstrip("/")
        self._model = str(self._cfg.get("gateway_model", "openclaw/default") or "openclaw/default")
        # Token resolution: explicit env wins, then config. Never logged.
        self._token = (
            os.environ.get("OPENCLAW_GATEWAY_TOKEN")
            or self._cfg.get("gateway_token")
            or ""
        )
        self._max_iterations = int(self._cfg.get("max_plan_depth", 5) or 5)

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """True when a gateway URL is configured (token optional if auth=none)."""
        return bool(self._base)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        payload: Dict[str, Any],
        timeout_ms: int = 30000,
        status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Run the OpenClaw planning loop and return a normalized result."""
        if not self._base:
            raise RuntimeError("OpenClaw gateway_url is not configured")

        deadline = time.monotonic() + max(1, timeout_ms) / 1000.0
        messages = self._build_messages(payload)
        tools: List[Dict[str, Any]] = payload.get("tools") or []
        max_iters = int(payload.get("max_plan_depth", self._max_iterations) or self._max_iterations)

        iterations = 0
        tools_executed = 0

        async with aiohttp.ClientSession() as session:
            for _ in range(max_iters):
                iterations += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("OpenClaw loop timed out after %d iteration(s)", iterations)
                    break

                # Force a client-tool call on the first turn so OpenClaw enters
                # our media-tool space instead of emitting its own internal
                # control tools (e.g. sessions_yield). Subsequent turns use
                # "auto" so it can synthesize a final prose answer.
                tool_choice = "required" if iterations == 1 and tools else "auto"
                message = await self._chat_once(
                    session, messages, tools, remaining, tool_choice=tool_choice
                )
                tool_calls = message.get("tool_calls") or []

                if not tool_calls:
                    content = self._extract_text(message)
                    return {
                        "status": "ok",
                        "response": content,
                        "iterations": iterations,
                        "model": self._model,
                        "raw": {"tools_executed": tools_executed},
                    }

                # Append the assistant turn that requested the tools, verbatim.
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )

                if status_callback:
                    await status_callback("Running media tools via OpenClaw")

                for tc in tool_calls:
                    tool_name, tool_args, call_id = self._parse_tool_call(tc)
                    if not tool_name:
                        continue
                    result = await self._fallback._execute_tool(tool_name, tool_args)
                    tools_executed += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "content": self._dump(result),
                        }
                    )

        # Exhausted plan depth / timeout: make one final, tool-free request so
        # OpenClaw can synthesize an answer from what it gathered.
        try:
            async with aiohttp.ClientSession() as session:
                remaining = max(2.0, deadline - time.monotonic())
                final = await self._chat_once(session, messages, [], remaining)
                content = self._extract_text(final)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenClaw final synthesis failed: %s", exc)
            content = ""

        return {
            "status": "ok",
            "response": content,
            "iterations": iterations,
            "model": self._model,
            "raw": {"tools_executed": tools_executed, "exhausted": True},
        }

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _chat_once(
        self,
        session: aiohttp.ClientSession,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        timeout_s: float,
        tool_choice: str = "auto",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        timeout = aiohttp.ClientTimeout(total=max(2.0, timeout_s))
        try:
            async with session.post(
                f"{self._base}/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=timeout,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"OpenClaw gateway HTTP {resp.status}: {text[:300]}")
                data = json.loads(text)
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"OpenClaw gateway request failed: {exc!r}") from exc
        except TimeoutError as exc:
            raise RuntimeError(
                f"OpenClaw gateway timed out after {timeout_s:.0f}s"
            ) from exc

        err = data.get("error")
        if err:
            raise RuntimeError(f"OpenClaw gateway error: {err}")

        choices = data.get("choices") or []
        if not choices:
            return {}
        return choices[0].get("message") or {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_messages(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        query = str(payload.get("query", "") or "")
        context_blocks: List[str] = []
        for label, key in (
            ("Conversation so far", "conversation_context"),
            ("Relevant transcript excerpts", "transcript_context"),
            ("Semantic search context", "semantic_context"),
        ):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                context_blocks.append(f"## {label}\n{val.strip()}")

        temporal = str(payload.get("temporal", "") or "")
        systems = payload.get("systems") or []
        hint_lines = []
        if systems:
            hint_lines.append(f"Active systems: {', '.join(systems)}.")
        if temporal:
            hint_lines.append(f"Temporal focus: {temporal}.")

        system_parts = [
            "You are the planning brain for a home-media assistant. Use the "
            "provided tools to gather recordings, schedule, transcript and "
            "system data before answering. Only report facts returned by tools; "
            "never invent titles, dates or results. When you have enough "
            "information, reply with a concise final answer for the user.",
        ]
        if hint_lines:
            system_parts.append(" ".join(hint_lines))
        if context_blocks:
            system_parts.append("\n\n".join(context_blocks))

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": "\n\n".join(system_parts)},
            {"role": "user", "content": query},
        ]
        return messages

    @staticmethod
    def _parse_tool_call(tc: Dict[str, Any]) -> tuple[str, Dict[str, Any], str]:
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name") or ""
        call_id = tc.get("id") or tc.get("call_id") or name
        raw_args = fn.get("arguments", tc.get("arguments"))
        args: Dict[str, Any]
        if isinstance(raw_args, dict):
            args = raw_args
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
                args = parsed if isinstance(parsed, dict) else {"value": parsed}
            except Exception:
                args = {}
        else:
            args = {}
        return name, args, call_id

    @staticmethod
    def _extract_text(message: Dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        return "" if content is None else str(content)

    @staticmethod
    def _dump(result: Any) -> str:
        try:
            return json.dumps(result, default=str)
        except Exception:
            return str(result)
