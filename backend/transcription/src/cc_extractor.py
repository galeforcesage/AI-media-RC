"""
cc_extractor.py
Closed Caption extraction for the transcription pipeline.

Strategy:
  1. Try `ccextractor` first - best for ATSC/MPEG-TS CEA-608/708 (most OTA recordings).
  2. Fall back to ffmpeg subtitle stream extraction for IPTV / explicit subtitle tracks.
  3. Return whisper-compatible segment list: [{"start", "end", "text"}, ...].
  4. analyze() computes coverage %, max gap, etc. so the worker can decide CC-only
     vs STT-full vs gap-fill.

Designed to be cheap to call: ccextractor reads only a small portion of the stream
to detect captions, and exits quickly if no CC is present.
"""

from __future__ import annotations
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Speaker-prefix patterns commonly seen in CC streams.
#   ">> JOHN:" or ">>JOHN:"  -> ATSC convention
#   "[NARRATOR]"             -> bracketed labels
#   "<v Speaker1>"           -> WebVTT voice tag
_SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?:>>\s*([A-Z][A-Z0-9 .'\-]{1,30})\s*:|\[([A-Z][A-Z0-9 .'\-]{1,30})\]|<v\s+([^>]{1,40})>)\s*",
    re.IGNORECASE,
)


def is_available() -> bool:
    """Return True if at least one CC extraction tool is installed."""
    return shutil.which("ccextractor") is not None or shutil.which("ffmpeg") is not None


def _have_ccextractor() -> bool:
    return shutil.which("ccextractor") is not None


def _parse_srt(srt_text: str) -> List[Dict]:
    """Parse SRT text into [{start, end, text}, ...] segments."""
    segments: List[Dict] = []
    if not srt_text or not srt_text.strip():
        return segments
    # Normalize line endings
    srt_text = srt_text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        # First line is index (skip), second is timecode "00:00:01,234 --> 00:00:04,567"
        timecode_line = None
        text_start_idx = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                timecode_line = ln
                text_start_idx = i + 1
                break
        if not timecode_line or text_start_idx is None:
            continue
        m = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            timecode_line.strip(),
        )
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        text = " ".join(lines[text_start_idx:]).strip()
        # Strip simple HTML/font tags ccextractor sometimes emits
        text = re.sub(r"<[^>]+>", "", text).strip()
        if not text:
            continue
        segments.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    return segments


def _parse_webvtt(vtt_text: str) -> List[Dict]:
    """Parse WebVTT text into segments."""
    segments: List[Dict] = []
    if not vtt_text or not vtt_text.strip():
        return segments
    vtt_text = vtt_text.replace("\r\n", "\n").replace("\r", "\n")
    # Drop the WEBVTT header and any NOTE blocks
    body = re.sub(r"^WEBVTT[^\n]*\n", "", vtt_text)
    blocks = re.split(r"\n\s*\n", body.strip())
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if lines[0].startswith("NOTE"):
            continue
        # Find the timecode line
        timecode_line = None
        text_start_idx = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                timecode_line = ln
                text_start_idx = i + 1
                break
        if not timecode_line or text_start_idx is None:
            continue
        m = re.match(
            r"(\d+):(\d+):(\d+)[.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[.,](\d+)",
            timecode_line.strip(),
        )
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        text = " ".join(lines[text_start_idx:]).strip()
        text = re.sub(r"<[^>]+>", "", text).strip()
        if not text:
            continue
        segments.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    return segments


def _run_ccextractor(video_path: str) -> Optional[List[Dict]]:
    """Try ccextractor; returns parsed segments or None on failure/no CC."""
    if not _have_ccextractor():
        return None
    with tempfile.NamedTemporaryFile(suffix=".srt", delete=False) as tmp:
        out_path = tmp.name
    try:
        # Minimal flags for max compatibility across ccextractor versions:
        # -o output, -out=srt format. Skip -nofontcolor / -quiet (rejected
        # by some apt-shipped builds). Font tags are stripped by _parse_srt().
        cmd = [
            "nice", "-n", "19", "ionice", "-c", "3",
            "ccextractor", video_path,
            "-o", out_path,
            "-out=srt",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            logger.debug("ccextractor exit %d: %s", proc.returncode, proc.stderr[-300:] if proc.stderr else "")
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            return None
        with open(out_path, "r", encoding="utf-8", errors="replace") as f:
            srt = f.read()
        segments = _parse_srt(srt)
        if not segments:
            return None
        return segments
    except subprocess.TimeoutExpired:
        logger.warning("ccextractor timed out for %s", video_path)
        return None
    except Exception:
        logger.exception("ccextractor failed for %s", video_path)
        return None
    finally:
        try:
            if os.path.exists(out_path):
                os.unlink(out_path)
        except OSError:
            pass


def _run_ffmpeg_subtitles(video_path: str) -> Optional[List[Dict]]:
    """Try to extract a subtitle stream via ffmpeg (WebVTT/SRT/etc)."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return None
    # Probe for subtitle streams
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "s",
                "-show_entries", "stream=index,codec_name",
                "-of", "csv=p=0",
                video_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            return None
    except Exception:
        return None

    with tempfile.NamedTemporaryFile(suffix=".vtt", delete=False) as tmp:
        out_path = tmp.name
    try:
        cmd = [
            "nice", "-n", "19", "ionice", "-c", "3",
            "ffmpeg", "-loglevel", "error", "-y",
            "-i", video_path,
            "-map", "0:s:0",
            "-f", "webvtt",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            return None
        with open(out_path, "r", encoding="utf-8", errors="replace") as f:
            vtt = f.read()
        segments = _parse_webvtt(vtt)
        return segments or None
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg subtitle extraction timed out for %s", video_path)
        return None
    except Exception:
        logger.exception("ffmpeg subtitle extraction failed for %s", video_path)
        return None
    finally:
        try:
            if os.path.exists(out_path):
                os.unlink(out_path)
        except OSError:
            pass


def extract(video_path: str) -> Optional[List[Dict]]:
    """Try to extract closed captions from a video file.

    Returns a list of {"start", "end", "text"} segments, or None if no CC is
    found or extraction failed. Caller should treat None as "no CC; fall back
    to STT".
    """
    if not os.path.exists(video_path):
        return None
    # Try ccextractor first (best for ATSC/MPEG-TS).
    segments = _run_ccextractor(video_path)
    if segments:
        logger.info("CC extracted via ccextractor: %d segments from %s", len(segments), os.path.basename(video_path))
        return segments
    # Fall back to ffmpeg subtitle stream.
    segments = _run_ffmpeg_subtitles(video_path)
    if segments:
        logger.info("CC extracted via ffmpeg: %d segments from %s", len(segments), os.path.basename(video_path))
        return segments
    return None


def analyze(segments: List[Dict], total_duration: float) -> Dict:
    """Compute coverage stats over a CC segment list.

    Returns:
        {
            "segment_count": int,
            "covered_seconds": float,    # sum of (end - start)
            "coverage_pct": float,       # 0-100
            "max_gap": float,            # longest silence between segments (seconds)
            "avg_gap": float,            # mean gap between consecutive segments
            "first_start": float,        # offset of first caption
            "last_end": float,           # offset of last caption
        }
    """
    if not segments or total_duration <= 0:
        return {
            "segment_count": 0, "covered_seconds": 0.0, "coverage_pct": 0.0,
            "max_gap": 0.0, "avg_gap": 0.0, "first_start": 0.0, "last_end": 0.0,
        }
    sorted_segs = sorted(segments, key=lambda s: s["start"])
    covered = sum(max(0.0, s["end"] - s["start"]) for s in sorted_segs)
    gaps: List[float] = []
    # Leading gap (from 0 to first caption)
    gaps.append(sorted_segs[0]["start"])
    for prev, cur in zip(sorted_segs, sorted_segs[1:]):
        gap = cur["start"] - prev["end"]
        if gap > 0:
            gaps.append(gap)
    # Trailing gap (from last caption to end of file)
    trailing = total_duration - sorted_segs[-1]["end"]
    if trailing > 0:
        gaps.append(trailing)
    max_gap = max(gaps) if gaps else 0.0
    avg_gap = sum(gaps) / len(gaps) if gaps else 0.0
    return {
        "segment_count": len(sorted_segs),
        "covered_seconds": round(covered, 2),
        "coverage_pct": round(100.0 * covered / total_duration, 1),
        "max_gap": round(max_gap, 2),
        "avg_gap": round(avg_gap, 2),
        "first_start": round(sorted_segs[0]["start"], 2),
        "last_end": round(sorted_segs[-1]["end"], 2),
    }


def strip_speaker_prefix(text: str) -> Tuple[str, Optional[str]]:
    """Strip a CC speaker prefix from text and normalize whitespace.

    Returns (clean_text, speaker_hint or None). Used by Phase 3 to auto-fill
    speaker maps. Phase 1 callers can use this just to keep displayed text clean.

    Also collapses runs of whitespace produced by CEA-608 positional padding
    (captions use spaces to position text on the screen, which would otherwise
    leak into the rendered transcript as ugly multi-space gaps).
    """
    if not text:
        return text, None
    # Collapse positional whitespace runs (incl. NBSP, tabs, newlines) to one space.
    text = re.sub(r"\s+", " ", text).strip()
    m = _SPEAKER_PREFIX_RE.match(text)
    if not m:
        return text, None
    hint = next((g for g in m.groups() if g), None)
    cleaned = text[m.end():].strip()
    if hint:
        hint = hint.strip().upper()
    return cleaned or text, hint


def find_gaps(
    segments: List[Dict],
    total_duration: float,
    min_gap: float = 6.0,
    pad: float = 0.5,
) -> List[Tuple[float, float]]:
    """Identify time regions with no CC coverage worth transcribing via STT.

    Returns sorted list of (start, end) tuples. Includes leading/trailing gaps.
    Each region is padded by `pad` seconds on both sides (clamped to file bounds)
    to avoid clipping word edges. Adjacent regions after padding are merged.
    Only gaps >= `min_gap` qualify.
    """
    if total_duration <= 0:
        return []
    sorted_segs = sorted(segments or [], key=lambda s: s["start"])
    raw_gaps: List[Tuple[float, float]] = []

    # Leading
    if not sorted_segs:
        return [(0.0, total_duration)] if total_duration >= min_gap else []
    if sorted_segs[0]["start"] >= min_gap:
        raw_gaps.append((0.0, sorted_segs[0]["start"]))
    # Between
    for prev, cur in zip(sorted_segs, sorted_segs[1:]):
        gap = cur["start"] - prev["end"]
        if gap >= min_gap:
            raw_gaps.append((prev["end"], cur["start"]))
    # Trailing
    if total_duration - sorted_segs[-1]["end"] >= min_gap:
        raw_gaps.append((sorted_segs[-1]["end"], total_duration))

    if not raw_gaps:
        return []

    # Pad and merge overlapping/adjacent
    padded: List[Tuple[float, float]] = []
    for s, e in raw_gaps:
        ps = max(0.0, s - pad)
        pe = min(total_duration, e + pad)
        if padded and ps <= padded[-1][1]:
            padded[-1] = (padded[-1][0], max(padded[-1][1], pe))
        else:
            padded.append((ps, pe))
    return padded


def merge_cc_stt(
    cc_segments: List[Dict],
    stt_segments: List[Dict],
) -> List[Dict]:
    """Merge CC + STT segments into a single sorted list, tagging source.

    CC segments take precedence in their time range. STT segments that overlap
    a CC segment by more than 50% are dropped (CC already covers that audio).
    """
    cc_tagged = [{**s, "source": "cc"} for s in (cc_segments or [])]
    cc_sorted = sorted(cc_tagged, key=lambda s: s["start"])

    kept_stt: List[Dict] = []
    for stt in stt_segments or []:
        stt_dur = max(0.001, stt["end"] - stt["start"])
        overlap = 0.0
        for cc in cc_sorted:
            if cc["end"] <= stt["start"]:
                continue
            if cc["start"] >= stt["end"]:
                break
            overlap += min(cc["end"], stt["end"]) - max(cc["start"], stt["start"])
        if overlap / stt_dur < 0.5:
            kept_stt.append({**stt, "source": "stt"})

    merged = sorted(cc_sorted + kept_stt, key=lambda s: s["start"])
    return merged
