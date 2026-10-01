from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from services.determine_filters import (  # noqa: E402
    BACKEND_CAPABILITIES,
    DateRange,
    DetermineFilters,
    MediaSystem,
    UnsupportedFilterError,
    WatchedState,
    compile_filters,
    extract_filters,
    map_date_filter,
    resolve_date_range,
    validate_capabilities,
)


# ── Spec FILTER_CASES ──────────────────────────────────────────────────────

FILTER_CASES = [
    {
        "text": "show Columbo recordings from last week",
        "expected": {"title": "Columbo", "recorded_expression": "last week"},
    },
    {
        "text": "find Murder by the Book",
        "expected": {"episode_title": "Murder by the Book"},
    },
    {
        "text": "shows starring Peter Falk",
        "expected": {"actor": "Peter Falk"},
    },
    {
        "text": "where did they mention Peter Falk",
        "expected": {"transcript_query": "Peter Falk", "actor": None},
    },
    {
        "text": (
            "transcripts from shows with Peter Falk "
            "recorded last week that mention fishing"
        ),
        "expected": {
            "actor": "Peter Falk",
            "transcript_query": "fishing",
            "recorded_expression": "last week",
        },
    },
    {
        "text": "shows with the character Columbo",
        "expected": {"character": "Columbo"},
        "expected_unsupported": set(),
    },
    {
        "text": "shows that originally aired in 1974",
        "expected": {"original_air_date": "1974"},
        "expected_unsupported": set(),
    },
    {
        "text": "uh show show me Law and Order from yesterday",
        "expected": {"title": "Law and Order", "recorded_expression": "yesterday"},
    },
]


@pytest.mark.parametrize("case", FILTER_CASES, ids=[c["text"][:32] for c in FILTER_CASES])
def test_filter_cases(case):
    f = extract_filters(case["text"])
    for key, want in case.get("expected", {}).items():
        if key == "recorded_expression":
            assert f.recorded_between is not None
            assert f.recorded_between.expression == want
        else:
            assert getattr(f, key) == want, f"{key}: {getattr(f, key)!r} != {want!r}"
    if "expected_unsupported" in case:
        assert f.unsupported_requests() == case["expected_unsupported"]


# ── Additional coverage required by the spec ───────────────────────────────


def test_actor_versus_transcript_mention():
    actor = extract_filters("shows starring Peter Falk")
    assert actor.actor == "Peter Falk"
    assert actor.transcript_query is None

    mention = extract_filters("where did they mention Peter Falk")
    assert mention.transcript_query == "Peter Falk"
    assert mention.actor is None


def test_series_title_versus_character_name():
    title = extract_filters("show me Columbo episodes")
    assert title.title == "Columbo"
    assert title.character is None

    character = extract_filters("shows with the character Columbo")
    assert character.character == "Columbo"
    # Character is now filterable server-side (SageTV cast-role list), so it
    # is no longer reported as unsupported.
    assert "character" not in character.unsupported_requests()
    # Must not silently become an actor or title.
    assert character.actor is None
    assert character.title is None


def test_actor_plus_transcript_plus_date():
    f = extract_filters(
        "transcripts from shows with Peter Falk recorded last week that mention fishing"
    )
    assert f.actor == "Peter Falk"
    assert f.transcript_query == "fishing"
    assert f.recorded_between.expression == "last week"


def test_date_range_plus_actor_and_genre():
    f = extract_filters("mysteries with Peter Falk from last month")
    assert f.genre == "Mystery"
    assert f.actor == "Peter Falk"
    assert f.recorded_between.expression == "last month"


def test_explicit_system_restriction():
    sage = extract_filters("show me SageTV recordings from yesterday")
    assert sage.systems == [MediaSystem.SAGETV]
    ch = extract_filters("what did Channels DVR record yesterday")
    assert MediaSystem.CHANNELS_DVR in ch.systems


def test_channel_name_versus_number():
    named = extract_filters("mysteries on MeTV")
    assert named.genre == "Mystery"
    assert named.channel == "MeTV"
    numbered = extract_filters("recordings on channel 5.1")
    assert numbered.channel == "5.1"


def test_empty_transcript_query_with_metadata_filters():
    # Metadata-only transcript intent: actor set, no transcript text.
    f = extract_filters("transcripts for shows with Peter Falk")
    assert f.actor == "Peter Falk"
    assert f.transcript_query is None


def test_original_air_date_year_is_filterable():
    f = extract_filters("shows that originally aired in 1974")
    # A concrete year is now server-side filterable, not unsupported.
    assert f.original_air_date == "1974"
    assert "original_air_date" not in f.unsupported_requests()
    # Must NOT become a recording-date filter.
    assert f.recorded_between is None
    res = compile_filters(f, now=datetime(2026, 9, 30))
    for call in res.calls:
        assert call.args.get("original_air_year") == 1974


# ── Capability compiler ────────────────────────────────────────────────────


def test_validate_capabilities_rejects_unsupported():
    with pytest.raises(UnsupportedFilterError):
        validate_capabilities({"season"}, "channels_upcoming")
    # Supported set passes silently.
    validate_capabilities({"title", "channel"}, "channels_upcoming")


def test_compile_emits_one_call_per_system():
    f = extract_filters("show Columbo recordings from last week")
    res = compile_filters(f, now=datetime(2026, 9, 30))
    targets = {c.target for c in res.calls}
    assert targets == {"sagetv_recordings", "channels_recordings"}
    for call in res.calls:
        assert call.args["title"] == "Columbo"
        assert call.args["start_date"] and call.args["end_date"]


def test_compile_transcript_search_single_call():
    f = extract_filters("where did they mention Peter Falk recorded last week")
    res = compile_filters(f, now=datetime(2026, 9, 30))
    assert len(res.calls) == 1
    call = res.calls[0]
    assert call.target == "transcript_search"
    assert call.args["query"] == "Peter Falk"
    assert "date_from" in call.args and "date_to" in call.args


def test_compile_respects_active_systems():
    f = extract_filters("what recorded yesterday")
    res = compile_filters(f, active_systems=["sagetv"], now=datetime(2026, 9, 30))
    assert {c.target for c in res.calls} == {"sagetv_recordings"}


def test_character_routes_to_sagetv_only():
    # Character filtering is SageTV-only (Channels has no character metadata),
    # so a character query must emit a SageTV call carrying the filter and
    # must NOT dump the Channels library with an unconstrained call.
    f = extract_filters("shows with the character Columbo")
    res = compile_filters(f, now=datetime(2026, 9, 30))
    targets = {c.target for c in res.calls}
    assert "sagetv_recordings" in targets
    assert "channels_recordings" not in targets
    sage = next(c for c in res.calls if c.target == "sagetv_recordings")
    assert sage.args["character"] == "Columbo"
    # Nothing unsupported: the request was fully honored by SageTV.
    assert not res.unsupported


def test_character_query_does_not_capture_interrogative_as_title():
    # "what recordings do I have with the character X" must NOT set title
    # to the interrogative "what" (which would dump the Channels library).
    f = extract_filters("what recordings do I have with the character Himself")
    assert f.character == "Himself"
    assert f.title is None
    res = compile_filters(f, now=datetime(2026, 9, 30))
    assert {c.target for c in res.calls} == {"sagetv_recordings"}
    assert res.calls[0].args.get("character") == "Himself"
    assert "title" not in res.calls[0].args


def test_original_air_date_without_year_is_reported_unsupported():
    # "originally aired" with no concrete year can't be resolved to a filter.
    f = extract_filters("shows that originally aired")
    assert f.original_air_date == "requested"
    assert "original_air_date" in f.unsupported_requests()
    res = compile_filters(f, now=datetime(2026, 9, 30))
    assert "original_air_date" in res.unsupported
    for call in res.calls:
        assert "original_air_year" not in call.args


def test_unwatched_filter_never_emitted():
    # Unwatched is a known-broken backend filter — must not be sent.
    f = extract_filters("unwatched recordings from yesterday")
    assert f.watched is WatchedState.UNWATCHED
    res = compile_filters(f, now=datetime(2026, 9, 30))
    for call in res.calls:
        assert "watched" not in call.args


def test_watched_only_is_emitted():
    f = DetermineFilters(watched=WatchedState.WATCHED)
    res = compile_filters(f)
    for call in res.calls:
        assert call.args.get("watched") is True


# ── Date resolution ────────────────────────────────────────────────────────


def test_resolve_yesterday():
    now = datetime(2026, 9, 30, 12, 0, 0)
    start, end = resolve_date_range("yesterday", now=now)
    assert start.strftime("%Y-%m-%d") == "2026-09-29"
    assert end.strftime("%Y-%m-%d") == "2026-09-29"


def test_resolve_rolling_week():
    now = datetime(2026, 9, 30, 12, 0, 0)
    start, end = resolve_date_range("last week", now=now)
    assert start.strftime("%Y-%m-%d") == "2026-09-23"
    assert end.strftime("%Y-%m-%d") == "2026-09-30"


def test_resolve_previous_calendar_week():
    now = datetime(2026, 9, 30, 12, 0, 0)  # Wednesday
    start, end = resolve_date_range("previous week", now=now)
    # Prior Sun–Sat: 2026-09-20 .. 2026-09-26
    assert start.strftime("%Y-%m-%d") == "2026-09-20"
    assert end.strftime("%Y-%m-%d") == "2026-09-26"


def test_map_date_filter_target_names():
    f = DetermineFilters(recorded_between=DateRange(expression="yesterday"))
    f = compile_filters(f, now=datetime(2026, 9, 30)).calls[0].args
    assert "start_date" in f and "end_date" in f


def test_transcript_uses_date_from_to():
    f = DetermineFilters(
        transcript_query="fishing",
        recorded_between=DateRange(
            start_utc=datetime(2026, 9, 23), end_utc=datetime(2026, 9, 30)
        ),
    )
    args = map_date_filter(f, "transcript_search")
    assert set(args) == {"date_from", "date_to"}


def test_case_insensitive_and_disfluency_cleanup():
    f = extract_filters("uh uh show me COLUMBO recordings")
    assert f.title.lower() == "columbo"


def test_backend_capabilities_shape():
    # Guard against accidental edits to the capability matrix.
    assert "watched" in BACKEND_CAPABILITIES["sagetv_recordings"]
    assert "watched" not in BACKEND_CAPABILITIES["channels_upcoming"]
    assert "transcript_query" in BACKEND_CAPABILITIES["transcript_search"]


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("what are all the press your luck episodes I have", "press your luck"),
        ("how many NCIS episodes do I have", "NCIS"),
        ("do I have any Columbo episodes", "Columbo"),
        ("all the Jeopardy recordings I have", "Jeopardy"),
    ],
)
def test_inventory_phrasings_extract_clean_title(prompt, expected):
    f = extract_filters(prompt)
    assert f.title == expected
    # These are metadata/title lookups, not dialogue searches.
    assert f.transcript_query is None


def test_inventory_preamble_preserves_leading_the():
    # "The Voice"/"The Office" must not lose their leading article.
    assert extract_filters("show me The Voice recordings").title == "The Voice"
    assert extract_filters("recordings of The Office").title == "The Office"


def test_compile_upcoming_emits_upcoming_targets():
    # A future/scheduled query must compile to the *upcoming* tools, not the
    # past-recordings search, so it can answer "what's going to record".
    f = extract_filters("what shows are going to record over the next 7 days")
    res = compile_filters(f, upcoming=True, now=datetime(2026, 9, 30))
    targets = {c.target for c in res.calls}
    assert targets == {"sagetv_upcoming", "channels_upcoming"}
    tools = {c.tool for c in res.calls}
    assert tools == {
        "sagetv_get_upcoming_recordings",
        "channels_get_upcoming_recordings",
    }


def test_compile_upcoming_respects_active_systems():
    f = extract_filters("what is scheduled tonight")
    res = compile_filters(
        f, active_systems=["channelsdvr"], upcoming=True,
        now=datetime(2026, 9, 30),
    )
    assert {c.target for c in res.calls} == {"channels_upcoming"}


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("is Shark Tank scheduled to record next week", "Shark Tank"),
        ("will Survivor record next week", "Survivor"),
        ("did NCIS record last night", "NCIS"),
        ("is Law and Order scheduled tomorrow", "Law and Order"),
        ("when does Jeopardy record", "Jeopardy"),
    ],
)
def test_future_intent_title_is_extracted(prompt, expected):
    f = extract_filters(prompt)
    assert f.title == expected


@pytest.mark.parametrize(
    "prompt",
    [
        "what shows are going to record over the next 7 days",
        "what is scheduled tonight",
        "what did I record yesterday",
    ],
)
def test_generic_future_query_captures_no_title(prompt):
    # A generic scheduling question must not bind a bogus title.
    assert extract_filters(prompt).title is None


def test_compile_upcoming_title_filters_both_backends():
    # SageTV upcoming now honors a title filter too, so a titled scheduling
    # query must constrain both backends rather than dumping SageTV's library.
    f = extract_filters("is Shark Tank scheduled to record next week")
    res = compile_filters(f, upcoming=True, now=datetime(2026, 9, 30))
    assert {c.target for c in res.calls} == {
        "sagetv_upcoming", "channels_upcoming"
    }
    for call in res.calls:
        assert call.args.get("title") == "Shark Tank"


