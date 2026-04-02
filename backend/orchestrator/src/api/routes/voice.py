"""
voice.py
FastAPI routes for voice interaction.
Accepts audio input, returns transcription + LLM response + TTS audio path.
"""

from __future__ import annotations
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Any, Dict, Optional
import tempfile
import os

from services.llm_pipeline import LLMPipeline
from services.voice_session import VoiceSessionManager

router = APIRouter(prefix="/voice", tags=["voice"])
pipeline: LLMPipeline | None = None
session_manager: VoiceSessionManager | None = None


def init_router(
    llm_pipeline: LLMPipeline,
    voice_session_manager: VoiceSessionManager,
) -> None:
    """Bind services at startup."""
    global pipeline, session_manager
    pipeline = llm_pipeline
    session_manager = voice_session_manager


def _require_pipeline() -> LLMPipeline:
    if pipeline is None:
        raise HTTPException(status_code=500, detail="LLM pipeline not initialized")
    return pipeline


def _require_sessions() -> VoiceSessionManager:
    if session_manager is None:
        raise HTTPException(status_code=500, detail="Voice session manager not initialized")
    return session_manager


class TextQueryRequest(BaseModel):
    prompt: str
    synthesize: bool = True


class SessionCreateResponse(BaseModel):
    session_id: str


class SessionTextRequest(BaseModel):
    session_id: str
    text: str


@router.post("/query")
async def voice_query(audio: UploadFile = File(...)):
    """
    One-shot voice query: audio → transcription → LLM → TTS.
    Returns transcription, LLM response, and output audio path.
    """
    pipe = _require_pipeline()

    suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await audio.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await pipe.run_voice_query(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if "error" in result:
        raise HTTPException(status_code=422, detail=result)

    return result


@router.post("/text")
async def text_query(request: TextQueryRequest):
    """
    Text-only query through the LLM pipeline with optional TTS synthesis.
    """
    pipe = _require_pipeline()
    result = await pipe.run_text_query(request.prompt, synthesize=request.synthesize)

    if "error" in result:
        raise HTTPException(status_code=422, detail=result)

    return result


@router.post("/session/create")
async def create_session() -> SessionCreateResponse:
    """Create a new multi-turn voice session."""
    mgr = _require_sessions()
    session = mgr.create_session()
    return SessionCreateResponse(session_id=session.session_id)


@router.post("/session/voice")
async def session_voice(
    session_id: str = Form(...),
    audio: UploadFile = File(...),
):
    """
    Submit audio input within an existing voice session.
    Maintains conversation context across turns.
    """
    mgr = _require_sessions()

    suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await audio.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await mgr.handle_voice(session_id, tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if "error" in result:
        raise HTTPException(status_code=422, detail=result)

    return result


@router.post("/session/text")
async def session_text(request: SessionTextRequest):
    """
    Submit text input within an existing voice session.
    Maintains conversation context across turns.
    """
    mgr = _require_sessions()
    result = await mgr.handle_text(request.session_id, request.text)

    if "error" in result:
        raise HTTPException(status_code=422, detail=result)

    return result


@router.delete("/session/{session_id}")
async def close_session(session_id: str):
    """Close and remove a voice session."""
    mgr = _require_sessions()
    removed = mgr.close_session(session_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok", "session_id": session_id}
