"""
llm.py
LLM service backed by Ollama (local inference via HTTP API).
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, Optional

import aiohttp

from utils.logger import get_logger
from utils.tracing import span as trace_span
from utils.metrics import llm_requests_total, llm_request_duration, llm_errors_total

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are the AI Media Remote Control assistant. You help users manage their home media systems including SageTV DVR and Channels DVR.

You can answer questions about:
- Recorded shows, upcoming recordings, and program schedules
- Playback control (play, pause, stop, skip, seek, volume)
- System status, disk usage, and service health
- Transcript search across recorded content

When the user asks about recorded content, use the transcript excerpts provided in the context to give accurate, specific answers. Quote relevant timestamps and episode details when available.

Keep responses concise and conversational. If you don't have enough information, say so clearly."""


# Reserve CPU cores for MCP/playback/SSH — don't let Ollama use all of them.
# Use 75% of available cores (minimum 1) for LLM inference.
import os as _os
DEFAULT_NUM_THREADS = max(1, int((_os.cpu_count() or 4) * 0.75))

# Only allow one LLM inference at a time so queries queue up rather than
# compounding CPU pressure.
DEFAULT_MAX_CONCURRENT_LLM = 1

# Default LLM generation parameters
DEFAULT_TEMPERATURE = 0.7
DEFAULT_NUM_PREDICT = 512
DEFAULT_NUM_CTX = 4096


class LLMService:
    """LLM inference via Ollama HTTP API."""

    def __init__(
        self,
        model_path: str = "models/llm",
        base_url: str = "http://127.0.0.1:11434",
        model: str = "mistral:instruct",
        num_threads: Optional[int] = None,
        temperature: float = DEFAULT_TEMPERATURE,
        num_predict: int = DEFAULT_NUM_PREDICT,
        num_ctx: int = DEFAULT_NUM_CTX,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_LLM,
    ) -> None:
        self.model_path = model_path
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_threads = num_threads if num_threads is not None else DEFAULT_NUM_THREADS
        self.temperature = temperature
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.loaded = False
        self._lock = asyncio.Lock()
        self._inference_semaphore = asyncio.Semaphore(max_concurrent)

    async def load(self) -> None:
        """Verify Ollama is reachable and the model is available."""
        async with self._lock:
            if self.loaded:
                return
            logger.info("Connecting to Ollama at %s, model=%s", self.base_url, self.model)
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(f"{self.base_url}/api/tags") as resp:
                        if resp.status != 200:
                            logger.error("Ollama not reachable: HTTP %d", resp.status)
                            return
                        data = await resp.json()
                        models = [m["name"] for m in data.get("models", [])]
                        if self.model not in models:
                            logger.warning("Model %s not found. Available: %s", self.model, models)
                        else:
                            logger.info("Model %s available", self.model)
                self.loaded = True
                logger.info("LLM service ready (Ollama)")
            except Exception as exc:
                logger.error("Failed to connect to Ollama: %s", exc)

    async def unload(self) -> None:
        """Mark service as unloaded."""
        self.loaded = False
        logger.info("LLM service unloaded")

    async def generate(
        self,
        prompt: str,
        params: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        Generate a response via Ollama.

        Returns:
            Dict with status, prompt, response, and model.
        """
        logger.info("LLM generating response (prompt length=%d)", len(prompt))
        llm_requests_total.inc()
        start = time.time()
        try:
            async with trace_span("llm.generate", {"model": self.model, "prompt_length": len(prompt)}) as s:
                payload = {
                    "model": self.model,
                    "system": SYSTEM_PROMPT,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": self.temperature,
                        "num_predict": self.num_predict,
                        "num_thread": self.num_threads,
                    },
                }
                if params:
                    payload["options"].update(params)
                if "qwen3" in self.model:
                    payload["think"] = False
                timeout = aiohttp.ClientTimeout(total=300)
                async with self._inference_semaphore:
                  async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        f"{self.base_url}/api/generate", json=payload
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.error("Ollama error %d: %s", resp.status, body[:200])
                            s.set_error(f"HTTP {resp.status}")
                            llm_errors_total.inc()
                            llm_request_duration.observe(time.time() - start)
                            return {"error": f"Ollama HTTP {resp.status}: {body[:200]}"}
                        data = await resp.json()

                response_text = data.get("response", "").strip()
                s.set_attribute("response_length", len(response_text))
                llm_request_duration.observe(time.time() - start)
                logger.info("LLM response (%d chars): %s", len(response_text), response_text[:100])
                return {
                    "status": "ok",
                    "prompt": prompt,
                    "response": response_text,
                    "model": self.model,
                }
        except asyncio.TimeoutError:
            logger.error("LLM generation timed out")
            llm_errors_total.inc()
            llm_request_duration.observe(time.time() - start)
            return {"error": "LLM generation timed out (300s)"}
        except Exception as exc:
            logger.exception("LLM generation error")
            llm_errors_total.inc()
            llm_request_duration.observe(time.time() - start)
            return {"error": str(exc)}

    async def generate_chat(
        self,
        messages: list[Dict[str, Any]],
        params: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        Multi-turn chat via Ollama /api/chat.

        Args:
            messages: List of {"role": "system"|"user"|"assistant", "content": "..."}.
            params: Optional override parameters.

        Returns:
            Dict with status, response, and model.
        """
        logger.info("LLM chat generation (%d messages)", len(messages))
        try:
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.num_predict,
                    "num_ctx": self.num_ctx,
                    "num_thread": self.num_threads,
                },
            }
            if "qwen3" in self.model:
                payload["think"] = False
            timeout = aiohttp.ClientTimeout(total=300)
            async with self._inference_semaphore:
              async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/api/chat", json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Ollama chat error %d: %s", resp.status, body[:200])
                        return {"error": f"Ollama HTTP {resp.status}: {body[:200]}"}
                    data = await resp.json()

            message = data.get("message", {})
            response_text = message.get("content", "").strip()
            logger.info(
                "LLM chat response (%d chars): %s",
                len(response_text), response_text[:100],
            )
            return {
                "status": "ok",
                "response": response_text,
                "model": self.model,
            }
        except asyncio.TimeoutError:
            logger.error("LLM chat generation timed out")
            return {"error": "LLM generation timed out (300s)"}
        except Exception as exc:
            logger.exception("LLM chat generation error")
            return {"error": str(exc)}

    async def stream_chat(
        self,
        messages: list[Dict[str, Any]],
        token_callback=None,
        tools: list[Dict[str, Any]] | None = None,
        params: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        Streaming multi-turn chat via Ollama /api/chat.

        When ``tools`` is provided, Ollama may return a tool_calls response
        instead of text content.  In that case no tokens are streamed and
        the returned dict contains a ``"tool_calls"`` key.

        Streams tokens through token_callback as they arrive,
        and returns the final accumulated result dict.
        """
        logger.info("LLM streaming chat (%d messages, %d tools)",
                     len(messages), len(tools) if tools else 0)
        try:
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.num_predict,
                    "num_ctx": self.num_ctx,
                    "num_thread": self.num_threads,
                },
            }
            if params:
                payload["options"].update(params)
            if tools:
                payload["tools"] = tools
            # Disable qwen3 thinking mode — it wastes the token budget
            # on <think> reasoning and produces empty responses with many tools
            if "qwen3" in self.model:
                payload["think"] = False
            # Log payload size for debugging context overflow
            payload_json = json.dumps(payload)
            logger.info("stream_chat payload size: %d chars (~%d tokens est.)",
                        len(payload_json), len(payload_json) // 4)
            accumulated = []
            accumulated_tool_calls = []
            timeout = aiohttp.ClientTimeout(total=600)
            async with self._inference_semaphore:
              async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/api/chat", json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Ollama stream_chat error %d: %s", resp.status, body[:200])
                        return {"error": f"Ollama HTTP {resp.status}: {body[:200]}"}
                    chunk_count = 0
                    async for line in resp.content:
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chunk_count += 1
                        msg = chunk.get("message", {})
                        # Log first chunk and any chunk with unexpected fields
                        if chunk_count == 1 or (not msg.get("content") and not msg.get("tool_calls") and not chunk.get("done")):
                            logger.info("Ollama chunk #%d keys: %s, msg keys: %s, content repr: %s",
                                        chunk_count, list(chunk.keys()), list(msg.keys()),
                                        repr(msg.get("content", ""))[:200])
                        # Native tool calls
                        tc = msg.get("tool_calls")
                        if tc:
                            accumulated_tool_calls.extend(tc)
                        token = msg.get("content", "")
                        if token:
                            accumulated.append(token)
                            if token_callback:
                                await token_callback(token)
                        # Capture thinking content if present (qwen3 may think even with think=false)
                        thinking = msg.get("thinking", "")
                        if thinking:
                            logger.warning("Ollama returned thinking content (%d chars): %s",
                                           len(thinking), thinking[:100])
                        if chunk.get("done"):
                            # Log full message when response appears empty
                            if not accumulated and not accumulated_tool_calls:
                                logger.warning(
                                    "Ollama done with empty result — full message: %s",
                                    json.dumps(msg)[:500],
                                )
                            logger.info(
                                "Ollama stream done after %d chunks, eval_count=%s, prompt_eval_count=%s, msg_role=%s",
                                chunk_count,
                                chunk.get("eval_count", "?"),
                                chunk.get("prompt_eval_count", "?"),
                                msg.get("role", "?"),
                            )
                            break

            response_text = "".join(accumulated).strip()
            # Strip qwen3 thinking tags from response
            if "<think>" in response_text:
                import re as _re
                response_text = _re.sub(r"<think>.*?</think>\s*", "", response_text, flags=_re.DOTALL).strip()
            if accumulated_tool_calls:
                logger.info(
                    "LLM stream_chat returned %d native tool_calls",
                    len(accumulated_tool_calls),
                )
                return {
                    "status": "ok",
                    "response": response_text,
                    "tool_calls": accumulated_tool_calls,
                    "model": self.model,
                }

            logger.info(
                "LLM stream_chat done (%d chars): %s",
                len(response_text), response_text[:100],
            )
            return {
                "status": "ok",
                "response": response_text,
                "model": self.model,
            }
        except asyncio.TimeoutError:
            logger.error("LLM stream_chat timed out")
            return {"error": "LLM generation timed out (300s)"}
        except Exception as exc:
            logger.exception("LLM stream_chat error")
            return {"error": str(exc)}

    async def stream(
        self,
        prompt: str,
        params: Dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Stream tokens from Ollama as an async generator."""
        logger.info("LLM streaming response (prompt length=%d)", len(prompt))
        payload = {
            "model": self.model,
            "system": SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.num_predict,
                "num_thread": self.num_threads,
            },
        }
        try:
            timeout = aiohttp.ClientTimeout(total=300)
            async with self._inference_semaphore:
              async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/api/generate", json=payload
                ) as resp:
                    async for line in resp.content:
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            token = chunk.get("response", "")
                            if token:
                                yield token
                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception as exc:
            logger.exception("LLM streaming error")
            yield f"[Error: {exc}]"
