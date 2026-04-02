"""
ssd_extractor.py
Service for extracting scene/shot/segment descriptors (SSD) from text.
Uses a local LLM to parse unstructured text into structured metadata.
"""

from __future__ import annotations
import json
import logging
from typing import Any, Dict, List, Optional

from services.llm import LLMService

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = (
    "You are a structured data extraction engine. "
    "Given raw text describing media content, extract key fields as JSON. "
    "Return only valid JSON with no additional commentary."
)


class SSDExtractor:
    """
    Extract structured scene/shot/segment descriptors from text
    using local LLM inference.
    """

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm

    async def extract(
        self,
        text: str,
        schema_hint: str | None = None,
    ) -> Dict[str, Any]:
        """
        Extract structured data from raw text.

        Args:
            text: The input text to parse.
            schema_hint: Optional description of desired output fields.

        Returns:
            Dict with status and extracted data, or error details.
        """
        if not self.llm.loaded:
            logger.error("SSD extraction requested but LLM is not loaded")
            return {"error": "LLM not loaded"}

        if not text.strip():
            return {"error": "Empty input text"}

        prompt = self._build_prompt(text, schema_hint)
        logger.info("SSD extraction (text length=%d)", len(text))

        try:
            result = await self.llm.generate(prompt, params={"mode": "extract"})
            if result.get("status") != "ok":
                return {"error": "LLM generation failed", "detail": result}

            raw_response = result.get("response", "")
            parsed = self._try_parse_json(raw_response)

            return {
                "status": "ok",
                "raw": raw_response,
                "structured": parsed,
            }
        except Exception as exc:
            logger.exception("SSD extraction failed")
            return {"error": str(exc)}

    async def extract_scenes(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract a list of scene descriptors from text.

        Returns a list of dicts, each describing one scene/segment.
        """
        result = await self.extract(
            text,
            schema_hint=(
                "Extract a list of scenes. Each scene should have: "
                "title, start_time, end_time, description, characters, mood."
            ),
        )
        if "error" in result:
            return []

        structured = result.get("structured")
        if isinstance(structured, list):
            return structured
        if isinstance(structured, dict):
            return structured.get("scenes", [])
        return []

    def _build_prompt(self, text: str, schema_hint: str | None) -> str:
        """Assemble the extraction prompt."""
        parts = [EXTRACTION_SYSTEM_PROMPT, ""]
        if schema_hint:
            parts.append(f"Desired output schema: {schema_hint}")
            parts.append("")
        parts.append(f"Input text:\n{text}")
        parts.append("")
        parts.append("Extracted JSON:")
        return "\n".join(parts)

    @staticmethod
    def _try_parse_json(text: str) -> Any:
        """Attempt to parse a JSON response; return raw string on failure."""
        text = text.strip()
        # Try to find JSON in the response
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = text.find(start_char)
            end = text.rfind(end_char)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return text
