"""
llm_pipeline.py
End-to-end voice and text query pipeline.
Coordinates: audio → whisper → llm → tts
Supports model selection via metadata and streaming tokens.
"""

from __future__ import annotations
import logging
from typing import Any, AsyncIterator, Dict, Optional

from services.whisper import WhisperService
from services.llm import LLMService
from services.tts import TTSService
from services.transcription_query import TranscriptionQuery

logger = logging.getLogger(__name__)


class LLMPipeline:
    """
    Orchestrates the full inference pipeline for voice and text queries.

    Accepts a TranscriptionQuery or raw strings and routes through
    the whisper → llm → tts chain.
    """

    def __init__(
        self,
        whisper: WhisperService,
        llm: LLMService,
        tts: TTSService,
    ) -> None:
        self.whisper = whisper
        self.llm = llm
        self.tts = tts

    async def run_voice_query(
        self,
        audio_path: str,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        Full voice pipeline: transcription → LLM generation → TTS synthesis.

        Args:
            audio_path: Path to incoming audio file.
            metadata: Optional metadata for model selection or prompt augmentation.

        Returns:
            Dict with transcription, llm_response, and audio_path.
        """
        logger.info("Voice query pipeline started: %s", audio_path)

        transcription = await self.whisper.transcribe(audio_path)
        if transcription.get("status") != "ok":
            logger.error("Transcription stage failed")
            return {"error": "Transcription failed", "detail": transcription}

        text = transcription["text"]
        logger.info("Transcription complete: %s", text[:100])

        llm_result = await self.llm.generate(text, metadata=metadata)
        if llm_result.get("status") != "ok":
            logger.error("LLM generation stage failed")
            return {
                "error": "LLM generation failed",
                "transcription": text,
                "detail": llm_result,
            }

        response_text = llm_result["response"]
        logger.info("LLM response: %s", response_text[:100])

        tts_result = await self.tts.synthesize(response_text)
        if tts_result.get("status") != "ok":
            logger.error("TTS synthesis stage failed")
            return {
                "error": "TTS synthesis failed",
                "transcription": text,
                "llm_response": response_text,
                "detail": tts_result,
            }

        logger.info("Voice query pipeline complete")
        return {
            "status": "ok",
            "transcription": text,
            "llm_response": response_text,
            "audio_path": tts_result["audio_path"],
        }

    async def run_transcription_query(self, query: TranscriptionQuery) -> Dict[str, Any]:
        """
        Accept a TranscriptionQuery object and route it through the full pipeline.
        """
        logger.info("TranscriptionQuery pipeline started: %s", query.query_id)
        query = await self.whisper.transcribe_query(query)
        if query.error:
            return {"error": query.error, "query_id": query.query_id}

        llm_result = await self.llm.generate(query.text or "")
        if llm_result.get("status") != "ok":
            return {
                "error": "LLM generation failed",
                "transcription": query.text,
                "detail": llm_result,
                "query_id": query.query_id,
            }

        response_text = llm_result["response"]
        tts_result = await self.tts.synthesize(response_text)

        return {
            "status": "ok",
            "query_id": query.query_id,
            "transcription": query.text,
            "llm_response": response_text,
            "audio_path": tts_result.get("audio_path"),
        }

    async def run_text_query(
        self,
        prompt: str,
        synthesize: bool = True,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        Text-only pipeline: LLM generation with optional TTS synthesis.

        Args:
            prompt: The text prompt.
            synthesize: Whether to synthesize the response via TTS.
            metadata: Optional metadata for model selection.

        Returns:
            Dict with llm_response and optionally audio_path.
        """
        logger.info("Text query pipeline started (synthesize=%s)", synthesize)

        llm_result = await self.llm.generate(prompt, metadata=metadata)
        if llm_result.get("status") != "ok":
            logger.error("LLM generation stage failed")
            return {"error": "LLM generation failed", "detail": llm_result}

        response_text = llm_result["response"]
        result: Dict[str, Any] = {
            "status": "ok",
            "llm_response": response_text,
        }

        if synthesize:
            tts_result = await self.tts.synthesize(response_text)
            if tts_result.get("status") == "ok":
                result["audio_path"] = tts_result["audio_path"]

        logger.info("Text query pipeline complete")
        return result

    async def stream_text_query(self, prompt: str) -> AsyncIterator[str]:
        """
        Stream LLM tokens for a text query.

        Yields individual token strings.
        """
        logger.info("Streaming text query (prompt length=%d)", len(prompt))
        async for token in self.llm.stream(prompt):
            yield token
