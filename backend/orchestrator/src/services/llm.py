"""
llm.py
Local LLM orchestration service.
Handles model loading, prompt routing, model selection, and local inference.
Streams tokens back via an async generator when streaming is requested.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)


class LLMService:
    """Local LLM inference service supporting load/unload lifecycle and generation."""

    def __init__(self, model_path: str = "models/llm") -> None:
        self.model_path = model_path
        self.loaded = False
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Load the LLM model into memory."""
        async with self._lock:
            if self.loaded:
                logger.info("LLM model already loaded from %s", self.model_path)
                return
            logger.info("Loading LLM model from %s …", self.model_path)
            # Replace with actual model loading (llama-cpp-python, ctransformers, etc.)
            await asyncio.sleep(0)  # yield to event loop
            self._model = object()  # sentinel for loaded state
            self.loaded = True
            logger.info("LLM model loaded successfully")

    async def unload(self) -> None:
        """Release the LLM model from memory."""
        async with self._lock:
            self._model = None
            self.loaded = False
            logger.info("LLM model unloaded")

    async def generate(
        self,
        prompt: str,
        params: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        Run a single-shot generation.

        Args:
            prompt: The text prompt.
            params: Generation parameters (max_tokens, temperature, etc.).
            metadata: Optional metadata for model selection or tagging.

        Returns:
            Dict with status, prompt, response, and optional metadata.
        """
        if not self.loaded:
            logger.error("Generate called but LLM is not loaded")
            return {"error": "LLM not loaded"}

        logger.info("LLM generating response (prompt length=%d)", len(prompt))
        try:
            # Replace with actual inference call
            await asyncio.sleep(0)  # yield
            response_text = f"[LLM response to: {prompt[:80]}]"
            return {
                "status": "ok",
                "prompt": prompt,
                "response": response_text,
                "model": self.model_path,
            }
        except Exception as exc:
            logger.exception("LLM generation error")
            return {"error": str(exc)}

    async def stream(
        self,
        prompt: str,
        params: Dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """
        Stream tokens back as an async generator.

        Args:
            prompt: The text prompt.
            params: Generation parameters.

        Yields:
            Individual token strings.
        """
        if not self.loaded:
            logger.error("Stream called but LLM is not loaded")
            return

        logger.info("LLM streaming response (prompt length=%d)", len(prompt))
        # Replace with real streaming inference
        tokens = f"[LLM response to: {prompt[:80]}]".split()
        for token in tokens:
            await asyncio.sleep(0)
            yield token + " "
