"""
llm.py
LLM service backed by Ollama (local inference via HTTP API).
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

import aiohttp

from utils.logger import get_logger

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
# On a 16-core box, 12 inference threads leaves 4 for everything else.
DEFAULT_NUM_THREADS = 12

# Only allow one LLM inference at a time so queries queue up rather than
# compounding CPU pressure.
MAX_CONCURRENT_LLM = 1


class LLMService:
    """LLM inference via Ollama HTTP API."""

    def __init__(
        self,
        model_path: str = "models/llm",
        base_url: str = "http://127.0.0.1:11434",
        model: str = "mistral:instruct",
        num_threads: int = DEFAULT_NUM_THREADS,
    ) -> None:
        self.model_path = model_path
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_threads = num_threads
        self.loaded = False
        self._lock = asyncio.Lock()
        self._inference_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)

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
        try:
            payload = {
                "model": self.model,
                "system": SYSTEM_PROMPT,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.7,
                    "num_predict": 512,
                    "num_thread": self.num_threads,
                },
            }
            timeout = aiohttp.ClientTimeout(total=120)
            async with self._inference_semaphore:
              async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/api/generate", json=payload
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Ollama error %d: %s", resp.status, body[:200])
                        return {"error": f"Ollama HTTP {resp.status}: {body[:200]}"}
                    data = await resp.json()

            response_text = data.get("response", "").strip()
            logger.info("LLM response (%d chars): %s", len(response_text), response_text[:100])
            return {
                "status": "ok",
                "prompt": prompt,
                "response": response_text,
                "model": self.model,
            }
        except asyncio.TimeoutError:
            logger.error("LLM generation timed out")
            return {"error": "LLM generation timed out (120s)"}
        except Exception as exc:
            logger.exception("LLM generation error")
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
                    "temperature": 0.7,
                    "num_predict": 1024,
                    "num_ctx": 8192,
                    "num_thread": self.num_threads,
                },
            }
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
                "temperature": 0.7,
                "num_predict": 512,
                "num_thread": self.num_threads,
            },
        }
        try:
            timeout = aiohttp.ClientTimeout(total=120)
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
