"""
diarization.py
Speaker diarization using pyannote.audio.

Runs diarization on extracted WAV files and aligns speaker labels
to Whisper transcript segments. Optionally maps speaker labels
to character/role names from metadata.
"""

from __future__ import annotations
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .gpu import release_cuda_memory, cuda_is_usable

logger = logging.getLogger(__name__)

# pyannote needs a meaningful amount of audio; anything shorter is noise.
MIN_DIARIZATION_SECONDS = 0.5

# Lazy-loaded pyannote pipeline
_pipeline = None
_pipeline_failed = False


def _get_hf_token() -> Optional[str]:
    """Read HuggingFace token from env or file."""
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        return token
    token_path = os.path.expanduser("~/.huggingface/token")
    if os.path.isfile(token_path):
        with open(token_path) as f:
            return f.read().strip()
    return None


def is_available() -> bool:
    """Check if pyannote.audio is installed and usable."""
    global _pipeline_failed
    if _pipeline_failed:
        return False
    try:
        import pyannote.audio  # noqa: F401
        return True
    except ImportError:
        return False


def is_loaded() -> bool:
    """True if the diarization pipeline is currently resident in memory."""
    return _pipeline is not None


def unload() -> None:
    """Release the cached pyannote pipeline and any VRAM it holds.

    The next call to ``diarize()`` transparently reloads it.
    """
    global _pipeline
    if _pipeline is None:
        return
    logger.info("Unloading pyannote diarization pipeline to free memory")
    _pipeline = None
    release_cuda_memory()


def _load_pipeline():
    """Load the pyannote speaker diarization pipeline (once)."""
    global _pipeline, _pipeline_failed
    if _pipeline is not None:
        return _pipeline
    if _pipeline_failed:
        return None

    try:
        from pyannote.audio import Pipeline

        # pyannote 3.3.2 still passes the deprecated `use_auth_token=` kwarg
        # down to huggingface_hub.hf_hub_download, but huggingface_hub >= 1.0
        # removed that alias and only accepts `token=`. Wrap the function once
        # to translate the kwarg so the upstream Pipeline keeps working.
        try:
            from huggingface_hub import file_download as _hf_file_download
            _orig_dl = _hf_file_download.hf_hub_download
            if not getattr(_orig_dl, "_aimedia_patched", False):
                def _patched_dl(*args, **kwargs):
                    if "use_auth_token" in kwargs and "token" not in kwargs:
                        kwargs["token"] = kwargs.pop("use_auth_token")
                    elif "use_auth_token" in kwargs:
                        kwargs.pop("use_auth_token", None)
                    return _orig_dl(*args, **kwargs)
                _patched_dl._aimedia_patched = True  # type: ignore[attr-defined]
                _hf_file_download.hf_hub_download = _patched_dl
                # Also rebind the symbol that pyannote imported into its module.
                import huggingface_hub as _hf
                _hf.hf_hub_download = _patched_dl
                from pyannote.audio.core import pipeline as _pa_pipeline
                _pa_pipeline.hf_hub_download = _patched_dl
                # pyannote.audio.core.model also imports hf_hub_download
                # directly and uses it during Model.from_pretrained.
                try:
                    from pyannote.audio.core import model as _pa_model
                    _pa_model.hf_hub_download = _patched_dl
                except Exception:
                    logger.debug("pyannote.audio.core.model patch skipped", exc_info=True)
        except Exception:
            logger.debug("hf_hub_download patch skipped", exc_info=True)

        token = _get_hf_token()
        if not token:
            logger.warning(
                "No HuggingFace token found. Set HF_TOKEN env var or "
                "run `huggingface-cli login`. Diarization disabled."
            )
            _pipeline_failed = True
            return None

        logger.info("Loading pyannote speaker-diarization-3.1 pipeline...")
        start = time.time()
        # pyannote.audio 4.x renamed `use_auth_token` to `token`; 3.x only
        # accepts the old name. Try the modern kwarg and fall back so this
        # works against either major version.
        try:
            _pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=token,
            )
        except TypeError:
            _pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=token,
            )
        # pyannote defaults to CPU. Move to GPU only when torch can genuinely
        # run kernels here — see cuda_is_usable() for why is_available() alone
        # is not enough.
        if cuda_is_usable():
            try:
                import torch
                _pipeline.to(torch.device("cuda"))
                logger.info("Diarization pipeline moved to CUDA")
            except Exception:
                logger.exception("Could not move diarization pipeline to CUDA; staying on CPU")
        else:
            logger.info("Diarization pipeline running on CPU")
        logger.info("Diarization pipeline loaded in %.1fs", time.time() - start)
        return _pipeline

    except Exception:
        logger.exception("Failed to load diarization pipeline. Diarization disabled.")
        _pipeline_failed = True
        return None


def _load_audio_as_waveform(audio_path: str) -> Optional[dict]:
    """Load audio file as a waveform dict that pyannote can consume directly.

    This bypasses pyannote's built-in audio loading which requires torchcodec.
    Returns {"waveform": Tensor(channel, time), "sample_rate": int}, or None if
    the file holds too little audio to diarize.

    pyannote rejects any waveform where ``shape[0] > shape[1]``, so a zero-length
    file (left behind by a failed ffmpeg extraction) arrives as (1, 0) and blows
    up with a confusing "must be provided as a (channel, time) torch Tensor".
    Screen those out here instead.
    """
    import torch
    import soundfile as sf
    import numpy as np

    data, sample_rate = sf.read(audio_path, dtype="float32")
    if data.size == 0:
        logger.warning("Diarization skipped: %s contains no audio samples", audio_path)
        return None

    # soundfile returns (samples,) for mono or (samples, channels) for stereo
    if data.ndim == 1:
        data = data[np.newaxis, :]  # (1, samples)
    else:
        data = data.T  # (channels, samples)

    n_channels, n_samples = data.shape
    duration = n_samples / float(sample_rate or 1)
    if duration < MIN_DIARIZATION_SECONDS:
        logger.warning(
            "Diarization skipped: %s is only %.2fs (minimum %.2fs)",
            audio_path, duration, MIN_DIARIZATION_SECONDS,
        )
        return None
    if n_channels > n_samples:
        logger.warning(
            "Diarization skipped: %s has more channels (%d) than samples (%d)",
            audio_path, n_channels, n_samples,
        )
        return None

    # .T yields a non-contiguous view; make it contiguous so torch doesn't have
    # to copy it again downstream.
    waveform = torch.from_numpy(np.ascontiguousarray(data))
    return {"waveform": waveform, "sample_rate": int(sample_rate)}


def diarize(audio_path: str) -> List[Dict[str, Any]]:
    """
    Run speaker diarization on an audio file.

    Returns list of:
        {speaker: "SPEAKER_00", start: float, end: float}
    sorted by start time.
    """
    pipeline = _load_pipeline()
    if pipeline is None:
        return []

    logger.info("Running diarization on %s", audio_path)
    start = time.time()
    # Load audio ourselves to bypass torchcodec AudioDecoder issue
    audio_input = _load_audio_as_waveform(audio_path)
    if audio_input is None:
        return []
    result = pipeline(audio_input)
    elapsed = time.time() - start
    logger.info("Diarization complete in %.1fs", elapsed)

    # Newer pyannote returns DiarizeOutput wrapper; extract Annotation
    annotation = getattr(result, "speaker_diarization", result)

    turns = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append({
            "speaker": speaker,
            "start": round(turn.start, 2),
            "end": round(turn.end, 2),
        })
    turns.sort(key=lambda t: t["start"])

    n_speakers = len({t["speaker"] for t in turns})
    logger.info("Diarization found %d speakers, %d turns", n_speakers, len(turns))
    return turns


def align_speakers_to_segments(
    segments: List[Dict[str, Any]],
    turns: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Assign a speaker label to each Whisper segment based on diarization turns.

    Uses maximum overlap between the segment timespan and speaker turns.
    Modifies segments in-place and returns them.
    """
    if not turns:
        return segments

    for seg in segments:
        seg_start = seg["start"]
        seg_end = seg["end"]
        speaker_overlap: Dict[str, float] = {}

        for turn in turns:
            overlap_start = max(seg_start, turn["start"])
            overlap_end = min(seg_end, turn["end"])
            overlap = max(0.0, overlap_end - overlap_start)
            if overlap > 0:
                speaker_overlap[turn["speaker"]] = (
                    speaker_overlap.get(turn["speaker"], 0.0) + overlap
                )

        if speaker_overlap:
            seg["speaker"] = max(speaker_overlap, key=speaker_overlap.get)
        else:
            seg["speaker"] = None

    return segments


def map_speakers_to_characters(
    segments: List[Dict[str, Any]],
    actors: List[Dict[str, Any]],
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """
    Map anonymous speaker labels (SPEAKER_00, etc.) to character/role names.

    Strategy: rank speakers by total speaking time, rank actors by billing order,
    then map 1:1. This is a heuristic — the actor with highest billing in a show
    typically speaks the most.

    actors: list of {name: str, role: str | None, billing_order: int | None}

    Returns:
        (speaker_map, updated_segments)
        speaker_map: {"SPEAKER_00": "Character Name", ...}
    """
    if not actors or not segments:
        return {}, segments

    # Calculate total speaking time per speaker
    speaker_time: Dict[str, float] = {}
    for seg in segments:
        spk = seg.get("speaker")
        if spk:
            duration = seg["end"] - seg["start"]
            speaker_time[spk] = speaker_time.get(spk, 0.0) + duration

    if not speaker_time:
        return {}, segments

    # Sort speakers by total speaking time (most first)
    ranked_speakers = sorted(speaker_time.keys(), key=lambda s: -speaker_time[s])

    # Sort actors by billing order (prefer role/character name over actor name)
    sorted_actors = sorted(
        actors,
        key=lambda a: a.get("billing_order") or 9999,
    )

    # Build mapping: prefer character/role name, fall back to actor name
    speaker_map: Dict[str, str] = {}
    for i, spk in enumerate(ranked_speakers):
        if i < len(sorted_actors):
            actor = sorted_actors[i]
            # Prefer role/character name over actor name
            char_name = actor.get("role") or actor.get("name", spk)
            speaker_map[spk] = char_name
        # else: leave unmapped speakers with their SPEAKER_XX label

    # Apply mapping to segments
    for seg in segments:
        spk = seg.get("speaker")
        if spk and spk in speaker_map:
            seg["speaker"] = speaker_map[spk]

    logger.info("Speaker→character mapping: %s", speaker_map)
    return speaker_map, segments
