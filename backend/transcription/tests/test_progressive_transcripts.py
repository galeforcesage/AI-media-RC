from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models import TranscriptionJob
from src.progressive import caption_snapshot_is_usable
from src import cc_extractor
from src.cc_extractor import _parse_srt
from src.queue import TranscriptionQueue
from src.store import MetadataStore
from src.transcript_index import TranscriptIndex


def test_rejects_malformed_caption_snapshot():
    segments = [
        {"start": float(i), "end": float(i + 1), "text": "1062927H@9210627H@1092627H@10926"}
        for i in range(20)
    ]

    assert caption_snapshot_is_usable(segments) is False


def test_rejects_implausible_caption_timestamps():
    assert caption_snapshot_is_usable([
        {"start": 47_970.0, "end": 47_974.0, "text": "Valid looking words"}
    ]) is False


def test_ffmpeg_subcc_srt_tags_are_cleaned():
    segments = _parse_srt(
        "1\n00:00:01,000 --> 00:00:03,000\n"
        '<font face="Monospace">{\\an7}\\h\\hHello there.</font>\n'
    )

    assert segments == [{"start": 1.0, "end": 3.0, "text": "Hello there."}]


def test_caption_extraction_falls_back_to_embedded_subcc(monkeypatch, tmp_path):
    calls = []
    expected = [{"start": 1.0, "end": 2.0, "text": "Embedded caption"}]
    recording = tmp_path / "recording.mpg"
    recording.touch()

    monkeypatch.setattr(
        cc_extractor,
        "_run_ccextractor",
        lambda path: calls.append("ccextractor"),
    )
    monkeypatch.setattr(
        cc_extractor,
        "_run_ffmpeg_subtitles",
        lambda path: calls.append("subtitle_stream"),
    )
    monkeypatch.setattr(
        cc_extractor,
        "_run_ffmpeg_subcc",
        lambda path: calls.append("subcc") or expected,
    )

    assert cc_extractor.extract(str(recording)) == expected
    assert calls == ["ccextractor", "subtitle_stream", "subcc"]


def test_progressive_evidence_moves_live_to_stable_to_final(tmp_path):
    store = MetadataStore(str(tmp_path / "transcription.db"))
    store.open()
    try:
        store.ingest_evidence(
            "show-1",
            "sagetv",
            [
                {"start": 10.0, "end": 12.0, "text": "There was never a bug."},
                {"start": 12.0, "end": 15.0, "text": "It was a smoke screen."},
            ],
            evidence="cc",
        )
        live = store.get("show-1")
        assert live is not None
        assert live.transcript_state == "live"
        assert live.duration == 15.0
        assert live.segments[0]["cc_text"] == "There was never a bug."
        assert live.segments[0]["asr_text"] == ""

        store.ingest_evidence(
            "show-1",
            "sagetv",
            [
                {"start": 10.1, "end": 12.1, "text": "There was never a bug."},
                {"start": 12.1, "end": 15.1, "text": "It was a smoke screen."},
            ],
            evidence="asr",
        )
        stable = store.get("show-1")
        assert stable is not None
        assert stable.transcript_state == "stable"
        assert all(segment["state"] == "stable" for segment in stable.segments)
        assert stable.segments[0]["source"] == "cc_preferred"
        assert stable.segments[0]["asr_text"] == "There was never a bug."

        store.ingest_evidence(
            "show-1",
            "sagetv",
            [{"start": 10.05, "end": 12.05, "text": "There was never a bug."}],
            evidence="asr",
        )
        asr_count = store._conn.execute(
            "SELECT COUNT(*) FROM transcript_segments "
            "WHERE recording_id='show-1' AND segment_key LIKE 'asr:%'"
        ).fetchone()[0]
        assert asr_count == 2

        store.finalize_recording("show-1", "sagetv")
        final = store.get("show-1")
        assert final is not None
        assert final.transcript_state == "final"
        assert all(segment["state"] == "final" for segment in final.segments)
    finally:
        store.close()


def test_live_jobs_preempt_archival_backlog(tmp_path):
    queue = TranscriptionQueue(str(tmp_path / "transcription.db"))
    queue.open()
    try:
        queue.enqueue(TranscriptionJob(
            recording_id="old-show",
            system="sagetv",
            file_path="/recordings/old.mpg",
            priority=0,
        ))
        queue.enqueue(TranscriptionJob(
            recording_id="live-show__inc_100",
            system="channelsdvr",
            file_path="/recordings/live.mpg",
            priority=100,
        ))

        assert queue.dequeue().recording_id == "live-show__inc_100"
    finally:
        queue.close()


def test_final_job_queues_behind_active_live_job(tmp_path):
    queue = TranscriptionQueue(str(tmp_path / "transcription.db"))
    queue.open()
    try:
        live = TranscriptionJob(
            recording_id="show__inc_0",
            system="sagetv",
            file_path="/recordings/show.mpg",
            priority=100,
        )
        final = TranscriptionJob(
            recording_id="show__inc_100__final",
            system="sagetv",
            file_path="/recordings/show.mpg",
            priority=110,
        )
        queue.enqueue(live)
        queue.dequeue()
        queue.enqueue(final)

        queued = queue.get_job(final.job_id)
        assert queued is not None
        assert queued.status == "pending"
    finally:
        queue.close()


def test_live_segments_are_searchable_with_state(tmp_path):
    index = TranscriptIndex(str(tmp_path / "transcript_index.db"))
    try:
        index.upsert_live_transcript(
            "show-2",
            "channelsdvr",
            "/recordings/show-2.mpg",
            [{
                "start": 20.0,
                "end": 24.0,
                "text": "Frank is working for Roman.",
                "state": "live",
                "confidence": 0.65,
            }],
            transcript_state="live",
        )

        results = index.search_transcripts("Frank Roman")
        assert results
        assert results[0]["recording_id"] == "show-2"
        assert results[0]["transcript_state"] == "live"
    finally:
        index.close()
