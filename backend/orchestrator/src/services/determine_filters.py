"""
determine_filters.py
=====================

Phase 1 of the "Complete LLM Remote Filters" rework.

Converts a natural-language request into a normalized, validated filter
structure (``DetermineFilters``) and compiles that structure into the concrete
parameter names each backend MCP tool actually supports.

Design contract (see the spec):

* The planner (LLM) *identifies* possible filters, but Python must
  **validate** fields, **resolve** relative dates, **distinguish** metadata
  from transcript content, **drop** unsupported filters per backend, and
  **ask for clarification** when an entity is ambiguous.
* The planner must NOT calculate timestamps. It returns the original
  expression (``"yesterday"``, ``"last week"``) and deterministic Python
  resolves it using America/Chicago.
* Filtering happens in the MCP call, never by asking the model to filter the
  returned rows (the orchestrator strips several metadata fields before the
  model ever sees them).

Only *currently supported* backend filters are emitted. Fields that exist in
storage but are not yet server-side filterable (``character``,
``original_air_date``, ``duration``, ``rating`` …) are captured but reported as
unsupported rather than silently coerced into a different filter.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable

try:  # Python 3.11+
    from enum import StrEnum
except ImportError:  # pragma: no cover - fallback for <3.11
    class StrEnum(str, Enum):  # type: ignore
        pass

from pydantic import BaseModel, ConfigDict, Field

try:  # Optional: only used to widen date parsing at runtime on the server.
    from zoneinfo import ZoneInfo

    _CHICAGO = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover
    _CHICAGO = None


# A bare four-digit year (1900-2099) — the only ``original_air_date`` form we
# can currently resolve to a concrete server-side filter.
_YEAR_ONLY_RE = re.compile(r"(?:19|20)\d{2}")


# ══════════════════════════════════════════════════════════════════════════
# 1. Normalized filter model (intermediate representation)
# ══════════════════════════════════════════════════════════════════════════


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaSystem(StrEnum):
    SAGETV = "sagetv"
    CHANNELS_DVR = "channelsdvr"


class RecordingState(StrEnum):
    RECORDING = "recording"
    COMPLETE = "complete"


class WatchedState(StrEnum):
    ANY = "any"
    WATCHED = "watched"
    UNWATCHED = "unwatched"


class DateRange(StrictModel):
    expression: str | None = None
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    # Current tools filter recording dates, not original air date.
    field: str = "record_date"


class DetermineFilters(StrictModel):
    # Source selection
    systems: list[MediaSystem] = Field(default_factory=list)

    # Program identity
    title: str | None = None
    episode_title: str | None = None
    season: int | None = Field(default=None, ge=0)
    episode: int | None = Field(default=None, ge=0)

    # Program metadata
    actor: str | None = None
    genre: str | None = None
    channel: str | None = None

    # Recording-time constraint
    recorded_between: DateRange | None = None

    # Recording state
    watched: WatchedState = WatchedState.ANY
    archived: bool | None = None
    recording_state: RecordingState | None = None

    # Transcript content, separate from metadata
    transcript_query: str | None = None
    transcript_match_mode: str | None = None

    # Captured-but-unsupported (Phase 2). Preserved, never coerced into a
    # different filter. Surfaced via :meth:`unsupported_requests`.
    character: str | None = None
    original_air_date: str | None = None

    # Result controls
    limit: int = Field(default=50, ge=1, le=500)

    # Planning information
    confidence: float = Field(default=1.0, ge=0, le=1)
    clarification_reason: str | None = None

    # ── Derived helpers ────────────────────────────────────────────────

    def requested_fields(self) -> set[str]:
        """Filter field names the user actually constrained (non-empty)."""
        fields: set[str] = set()
        if self.title:
            fields.add("title")
        if self.episode_title:
            fields.add("episode_title")
        if self.season is not None:
            fields.add("season")
        if self.episode is not None:
            fields.add("episode")
        if self.actor:
            fields.add("actor")
        if self.genre:
            fields.add("genre")
        if self.channel:
            fields.add("channel")
        if self.recorded_between and (
            self.recorded_between.expression
            or self.recorded_between.start_utc
            or self.recorded_between.end_utc
        ):
            fields.add("recorded_between")
        if self.watched is not WatchedState.ANY:
            fields.add("watched")
        if self.archived is not None:
            fields.add("archived")
        if self.recording_state is not None:
            fields.add("recording_state")
        if self.transcript_query:
            fields.add("transcript_query")
        if self.character:
            fields.add("character")
        if self.original_air_date:
            fields.add("original_air_date")
        return fields

    def unsupported_requests(self) -> set[str]:
        """Requested filters that no backend can honor as given.

        ``character`` and ``original_air_date`` are now filterable server-side
        (SageTV cast-role list / episode original-air year). The only remaining
        honest gap is an ``original_air_date`` request with no concrete year to
        match on (e.g. "originally aired" with no year) — reported so the user
        knows the constraint was understood but couldn't be applied.
        """
        unsupported: set[str] = set()
        if self.original_air_date and not _YEAR_ONLY_RE.fullmatch(
            self.original_air_date.strip()
        ):
            unsupported.add("original_air_date")
        return unsupported

    def is_transcript_query(self) -> bool:
        return bool(self.transcript_query)


# ══════════════════════════════════════════════════════════════════════════
# 2. Backend capability matrix
# ══════════════════════════════════════════════════════════════════════════

BACKEND_CAPABILITIES: dict[str, set[str]] = {
    "sagetv_recordings": {
        "title", "episode_title", "actor", "character", "genre", "channel",
        "season", "episode", "recorded_between", "original_air_date",
        "watched", "archived", "recording_state", "limit",
    },
    "channels_recordings": {
        "title", "episode_title", "actor", "genre", "channel",
        "season", "episode", "recorded_between", "original_air_date",
        "watched", "limit",
    },
    "sagetv_upcoming": {
        "title", "channel", "recorded_between", "limit",
    },
    "channels_upcoming": {
        "title", "channel", "recorded_between", "limit",
    },
    "transcript_search": {
        "transcript_query", "actor", "genre", "channel",
        "recorded_between", "systems", "limit",
    },
}

# MCP tool name for each capability target.
TARGET_TOOL = {
    "sagetv_recordings": "sagetv_search_recordings",
    "channels_recordings": "channels_search_recordings",
    "sagetv_upcoming": "sagetv_get_upcoming_recordings",
    "channels_upcoming": "channels_get_upcoming_recordings",
    "transcript_search": "transcript_cross_search",
}

RECORDING_TARGETS = {
    MediaSystem.SAGETV: "sagetv_recordings",
    MediaSystem.CHANNELS_DVR: "channels_recordings",
}
UPCOMING_TARGETS = {
    MediaSystem.SAGETV: "sagetv_upcoming",
    MediaSystem.CHANNELS_DVR: "channels_upcoming",
}


class UnsupportedFilterError(ValueError):
    pass


def validate_capabilities(requested_filters: Iterable[str], target: str) -> None:
    """Raise if ``requested_filters`` includes anything ``target`` can't do."""
    supported = BACKEND_CAPABILITIES.get(target)
    if supported is None:
        raise UnsupportedFilterError(f"Unknown filter target: {target}")
    unsupported = set(requested_filters) - supported
    if unsupported:
        raise UnsupportedFilterError(
            f"{target} does not support: {sorted(unsupported)}"
        )


# ══════════════════════════════════════════════════════════════════════════
# 3. Deterministic date resolution (America/Chicago)
# ══════════════════════════════════════════════════════════════════════════
#
# The planner returns an expression string; Python resolves it. Kept
# self-contained so it needs no third-party parser for the common phrases.
# (dateparser is used only as a runtime fallback for exotic expressions.)


def _now_local() -> datetime:
    if _CHICAGO is not None:
        return datetime.now(_CHICAGO).replace(tzinfo=None)
    return datetime.now()


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start, end


def _week_bounds(ref: datetime, offset_weeks: int = 0) -> tuple[datetime, datetime]:
    """Sunday–Saturday calendar week containing ``ref`` shifted by weeks."""
    days_since_sun = (ref.weekday() + 1) % 7  # Mon=0 → 1 … Sun=6 → 0
    sunday = ref - timedelta(days=days_since_sun) + timedelta(weeks=offset_weeks)
    start = sunday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return start, end


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# Rolling "week" phrases → last 7 days (checked before calendar phrases).
_ROLLING_WEEK_RE = re.compile(r"\b(?:last|past)\s+week\b", re.I)
_PREVIOUS_WEEK_RE = re.compile(r"\bprevious\s+week\b", re.I)
_THIS_WEEK_RE = re.compile(r"\bthis\s+week\b", re.I)
_LAST_N_DAYS_RE = re.compile(r"\blast\s+(\d{1,3})\s+days?\b", re.I)
_N_DAYS_AGO_RE = re.compile(r"\b(\d{1,3})\s+days?\s+ago\b", re.I)
_LAST_MONTH_RE = re.compile(r"\blast\s+month\b", re.I)
_THIS_MONTH_RE = re.compile(r"\bthis\s+month\b", re.I)
_YEAR_MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\b",
    re.I,
)


def resolve_date_range(
    expression: str | None, now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    """Resolve a relative/absolute date expression to inclusive local bounds.

    Returns ``(start, end)`` naive local datetimes (America/Chicago) or
    ``(None, None)`` when the expression cannot be resolved here.
    """
    if not expression:
        return None, None
    exp = expression.strip().lower()
    ref = now or _now_local()

    # Single-day phrases first.
    if re.search(r"\b(?:yesterday|last\s+night|last\s+evening)\b", exp):
        return _day_bounds(ref - timedelta(days=1))
    if re.search(r"\b(?:today|tonight|this\s+morning|this\s+afternoon|"
                 r"this\s+evening|earlier\s+today)\b", exp):
        return _day_bounds(ref)

    m = _LAST_N_DAYS_RE.search(exp)
    if m:
        n = int(m.group(1))
        start, _ = _day_bounds(ref - timedelta(days=n))
        _, end = _day_bounds(ref)
        return start, end

    m = _N_DAYS_AGO_RE.search(exp)
    if m:
        return _day_bounds(ref - timedelta(days=int(m.group(1))))

    # Rolling week phrases ("last week", "past week", "over the last week",
    # "recent", "lately") → trailing 7 days. Checked before calendar week.
    if _ROLLING_WEEK_RE.search(exp) or re.search(
        r"\b(?:recent(?:ly)?|lately|past\s+7\s+days|last\s+7\s+days)\b", exp
    ):
        start, _ = _day_bounds(ref - timedelta(days=7))
        _, end = _day_bounds(ref)
        return start, end

    if _PREVIOUS_WEEK_RE.search(exp):
        return _week_bounds(ref, offset_weeks=-1)
    if _THIS_WEEK_RE.search(exp):
        return _week_bounds(ref, offset_weeks=0)

    if _LAST_MONTH_RE.search(exp):
        first_this = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = first_this - timedelta(seconds=1)
        last_month_start = last_month_end.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return last_month_start, last_month_end
    if _THIS_MONTH_RE.search(exp):
        start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            nxt = start.replace(year=start.year + 1, month=1)
        else:
            nxt = start.replace(month=start.month + 1)
        return start, nxt - timedelta(seconds=1)

    # Absolute ISO date(s): "2026-09-29" or "2026-09-20 to 2026-09-26".
    iso = re.findall(r"\d{4}-\d{2}-\d{2}", exp)
    if iso:
        try:
            s = datetime.strptime(iso[0], "%Y-%m-%d")
            e = datetime.strptime(iso[-1], "%Y-%m-%d")
            start, _ = _day_bounds(s)
            _, end = _day_bounds(e)
            return start, end
        except ValueError:
            pass

    # Bare month name → that month in the current (or inferred) year.
    m = _YEAR_MONTH_RE.search(exp)
    if m:
        month = _MONTHS[m.group(1).lower()]
        yr = ref.year
        ym = re.search(r"\b(19|20)\d{2}\b", exp)
        if ym:
            yr = int(ym.group(0))
        start = datetime(yr, month, 1)
        if month == 12:
            nxt = datetime(yr + 1, 1, 1)
        else:
            nxt = datetime(yr, month + 1, 1)
        return start, nxt - timedelta(seconds=1)

    # Runtime fallback: dateparser (present on the server, optional locally).
    try:
        from dateparser.search import search_dates

        found = search_dates(
            expression, settings={"PREFER_DATES_FROM": "past"}, languages=["en"]
        )
        if found:
            dt = found[0][1]
            return _day_bounds(dt.replace(tzinfo=None))
    except Exception:
        pass

    return None, None


def resolve_recorded_between(dr: DateRange | None, now: datetime | None = None) -> DateRange | None:
    """Fill ``start_utc``/``end_utc`` on a DateRange from its expression."""
    if dr is None:
        return None
    if dr.start_utc and dr.end_utc:
        return dr
    start, end = resolve_date_range(dr.expression, now=now)
    return dr.model_copy(update={"start_utc": start, "end_utc": end})


def map_date_filter(filters: "DetermineFilters", target: str) -> dict:
    """Translate the normalized date range to a backend's parameter names."""
    dr = filters.recorded_between
    if not dr or not dr.start_utc or not dr.end_utc:
        return {}
    start = dr.start_utc.strftime("%Y-%m-%d")
    end = dr.end_utc.strftime("%Y-%m-%d")
    if target == "transcript_search":
        return {"date_from": start, "date_to": end}
    if target in {
        "sagetv_recordings", "channels_recordings",
        "sagetv_upcoming", "channels_upcoming",
    }:
        return {"start_date": start, "end_date": end}
    raise ValueError(f"Unknown filter target: {target}")


# ══════════════════════════════════════════════════════════════════════════
# 4. Backend-capability compiler
# ══════════════════════════════════════════════════════════════════════════


class BackendCall(StrictModel):
    target: str
    tool: str
    args: dict
    dropped: list[str] = Field(default_factory=list)


class CompileResult(StrictModel):
    calls: list[BackendCall] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    clarification_reason: str | None = None


def _selected_systems(filters: DetermineFilters) -> list[MediaSystem]:
    return list(filters.systems) if filters.systems else [
        MediaSystem.SAGETV, MediaSystem.CHANNELS_DVR
    ]


def _build_args(filters: DetermineFilters, target: str) -> dict:
    """Build the concrete MCP arguments a target tool accepts."""
    supported = BACKEND_CAPABILITIES[target]
    args: dict = {}

    if "title" in supported and filters.title:
        args["title"] = filters.title
    if "episode_title" in supported and filters.episode_title:
        args["episode_title"] = filters.episode_title
    if "actor" in supported and filters.actor:
        args["actor"] = filters.actor
    if "character" in supported and filters.character:
        args["character"] = filters.character
    if "genre" in supported and filters.genre:
        args["genre"] = filters.genre
    if "channel" in supported and filters.channel:
        args["channel"] = filters.channel
    if "season" in supported and filters.season is not None:
        args["season"] = filters.season
    if "episode" in supported and filters.episode is not None:
        args["episode"] = filters.episode
    if "archived" in supported and filters.archived is not None:
        args["archived"] = filters.archived
    if "recording_state" in supported and filters.recording_state is not None:
        args["recording_state"] = filters.recording_state.value

    # Watched: only WATCHED is reliably honored server-side. UNWATCHED is a
    # known-broken backend filter (both tools normalize watched=false → no
    # filter), so we never emit it — callers must post-filter or withhold it.
    if "watched" in supported and filters.watched is WatchedState.WATCHED:
        args["watched"] = True

    if "transcript_query" in supported and filters.transcript_query:
        args["query"] = filters.transcript_query

    if "systems" in supported and filters.systems:
        args["system"] = (
            filters.systems[0].value if len(filters.systems) == 1 else None
        )
        if args["system"] is None:
            args.pop("system")

    if "recorded_between" in supported:
        args.update(map_date_filter(filters, target))

    if "original_air_date" in supported and filters.original_air_date:
        _yr = filters.original_air_date.strip()
        if _YEAR_ONLY_RE.fullmatch(_yr):
            args["original_air_year"] = int(_yr)

    if "limit" in supported:
        args["limit"] = filters.limit

    return args


def compile_filters(
    filters: DetermineFilters,
    active_systems: list[str] | None = None,
    *,
    upcoming: bool = False,
    now: datetime | None = None,
) -> CompileResult:
    """Compile a validated ``DetermineFilters`` into concrete backend calls.

    * Resolves the recording date range deterministically.
    * Emits one call per selected system for recording/upcoming searches, or a
      single transcript_search call when a transcript query is present.
    * Records — never silently drops — any material filter a backend cannot
      honor, and reports Phase-2 filters (character, original_air_date) as
      unsupported with a clarification instead of coercing them.
    """
    # Resolve dates up front so every target sees concrete bounds.
    if filters.recorded_between is not None:
        filters = filters.model_copy(
            update={"recorded_between": resolve_recorded_between(
                filters.recorded_between, now=now
            )}
        )

    result = CompileResult()

    # Phase-2 / not-yet-filterable requests → report, do not coerce.
    unsupported = sorted(filters.unsupported_requests())
    if unsupported:
        result.unsupported = unsupported
        pretty = ", ".join(unsupported)
        result.clarification_reason = (
            f"Filtering by {pretty} isn't supported yet — it exists in the "
            f"metadata but no backend accepts it as a search filter."
        )

    if filters.clarification_reason and not result.clarification_reason:
        result.clarification_reason = filters.clarification_reason

    # Restrict to the caller's active systems if provided.
    selected = _selected_systems(filters)
    if active_systems:
        active = {
            MediaSystem.SAGETV if s in ("sagetv", "sage") else
            MediaSystem.CHANNELS_DVR
            for s in active_systems
        }
        selected = [s for s in selected if s in active] or selected

    requested = filters.requested_fields()

    # ── Transcript search ──────────────────────────────────────────────
    if filters.is_transcript_query():
        target = "transcript_search"
        supported = BACKEND_CAPABILITIES[target]
        dropped = sorted(
            (requested - supported) - filters.unsupported_requests()
        )
        args = _build_args(filters, target)
        result.calls.append(
            BackendCall(target=target, tool=TARGET_TOOL[target],
                        args=args, dropped=dropped)
        )
        return result

    # ── Recording / upcoming search per system ─────────────────────────
    target_map = UPCOMING_TARGETS if upcoming else RECORDING_TARGETS
    # Honorable constraints the user actually asked for (minus result caps).
    honorable = (requested - filters.unsupported_requests()) - {"limit"}
    for system in selected:
        target = target_map[system]
        supported = BACKEND_CAPABILITIES[target]
        dropped = sorted(
            (requested - supported) - filters.unsupported_requests()
        )
        args = _build_args(filters, target)
        # If the user asked for a concrete constraint but this backend can
        # honor none of it, skip the call rather than dumping its whole
        # library (e.g. a character query against Channels, which has no
        # character metadata).
        _constraining = {k for k in args if k not in ("limit", "system")}
        if honorable and not _constraining:
            continue
        result.calls.append(
            BackendCall(target=target, tool=TARGET_TOOL[target],
                        args=args, dropped=dropped)
        )

    return result


# ══════════════════════════════════════════════════════════════════════════
# 5. Rule-based entity extraction
# ══════════════════════════════════════════════════════════════════════════
#
# Deterministic extractor used as the authoritative live path (the local 7B
# model is unreliable at argument selection). An LLM-produced partial can be
# merged/validated via :func:`filters_from_partial`.

_GENRES = {
    "mystery": "Mystery", "mysteries": "Mystery",
    "comedy": "Comedy", "comedies": "Comedy",
    "drama": "Drama", "dramas": "Drama",
    "sports": "Sports", "sport": "Sports",
    "news": "News",
    "documentary": "Documentary", "documentaries": "Documentary",
    "sitcom": "Sitcom", "sitcoms": "Sitcom",
    "thriller": "Thriller", "thrillers": "Thriller",
    "horror": "Horror",
    "romance": "Romance",
    "reality": "Reality",
    "western": "Western", "westerns": "Western",
    "crime": "Crime",
    "action": "Action",
}

_STOP_TITLE_TAIL = re.compile(
    r"\b(?:recordings?|episodes?|shows?|programs?|from|on|recorded|that|"
    r"starring|featuring|with|please)\b.*$",
    re.I,
)

# Temporal expressions we recognize, longest first so multi-word phrases win.
_TEMPORAL_PATTERNS = [
    r"over\s+the\s+(?:last|past)\s+week",
    r"this\s+(?:last|past)\s+week",
    r"in\s+the\s+(?:last|past)\s+week",
    r"last\s+\d{1,3}\s+days?",
    r"\d{1,3}\s+days?\s+ago",
    r"previous\s+week",
    r"this\s+week",
    r"last\s+week",
    r"past\s+week",
    r"last\s+month",
    r"this\s+month",
    r"last\s+night",
    r"yesterday",
    r"today",
    r"tonight",
    r"recently",
    r"lately",
    r"\d{4}-\d{2}-\d{2}(?:\s+to\s+\d{4}-\d{2}-\d{2})?",
]
_TEMPORAL_RE = re.compile("|".join(f"(?:{p})" for p in _TEMPORAL_PATTERNS), re.I)

_DISFLUENCY_RE = re.compile(r"\b(?:uh|um|er|erm|hmm|uhh|umm|like)\b", re.I)


def _clean_prompt(prompt: str) -> str:
    """Strip disfluencies and immediate duplicate words."""
    p = _DISFLUENCY_RE.sub(" ", prompt)
    p = re.sub(r"\s+", " ", p).strip()
    # Collapse immediate duplicate words ("show show" → "show").
    p = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", p, flags=re.I)
    return p


def _tidy(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().strip('"\u201c\u201d\'.,!?').strip()
    return v or None


def _clean_entity(value: str | None) -> str | None:
    """Trim a captured entity at the first trailing filter keyword."""
    v = _tidy(value)
    if not v:
        return None
    v = _STOP_TITLE_TAIL.sub("", v).strip()
    v = v.strip('"\u201c\u201d\'.,!?').strip()
    return v or None


def extract_filters(prompt: str) -> DetermineFilters:
    """Extract a normalized ``DetermineFilters`` from natural language.

    Uses explicit linguistic cues (see spec §4). A person's name alone is
    never assumed to be an actor — the surrounding phrase decides whether it's
    an actor filter or transcript content.
    """
    original = prompt or ""
    text = _clean_prompt(original)
    working = text  # spans get blanked as they are consumed

    f: dict = {}
    systems: list[MediaSystem] = []

    def consume(span: re.Match | None) -> None:
        nonlocal working
        if span:
            working = working[: span.start()] + " " + working[span.end():]

    # 1) Transcript content cues (said / mention / talk about / contains).
    m = re.search(
        r"\b(?:mention(?:ed|s)?|said|says?|talk(?:ed|s)?\s+about|"
        r"transcript\s+contains?|contains?\s+the\s+words?)\b\s+(.+?)"
        r"(?:\?|$|\bfrom\b|\brecorded\b|\bstarring\b|\bfeaturing\b)",
        working, re.I,
    )
    if m:
        f["transcript_query"] = _tidy(m.group(1))
        f["transcript_match_mode"] = "lexical"
        consume(m)

    # 2) Explicit system restriction.
    if re.search(r"\bsage\s*tv\b|\bon\s+sage\b", working, re.I):
        systems.append(MediaSystem.SAGETV)
    if re.search(r"\bchannels(?:\s+dvr)?\b", working, re.I):
        systems.append(MediaSystem.CHANNELS_DVR)

    # 3) Character (Phase-2, unsupported) — must precede actor/title so
    #    "the character Columbo" is not mistaken for a title or actor.
    m = re.search(
        r"\b(?:the\s+)?character\s+(?:named\s+|called\s+)?"
        r"([A-Z][\w'\u2019.-]*(?:\s+[A-Z][\w'\u2019.-]*)*)",
        working,
    )
    if not m:
        m = re.search(
            r"\bcharacter\s+(?:named\s+|called\s+)?([a-z][\w'\u2019.-]*"
            r"(?:\s+[a-z][\w'\u2019.-]*)*)",
            working, re.I,
        )
    if m:
        f["character"] = _tidy(m.group(1))
        consume(m)

    # 4) Original-air-date (Phase-2, unsupported).
    m = re.search(
        r"\b(?:originally\s+aired|first\s+aired|original\s+air\s+date)\b"
        r"(?:[^.?]*?\b((?:19|20)\d{2})\b)?",
        working, re.I,
    )
    if m:
        f["original_air_date"] = m.group(1) if m.group(1) else "requested"
        consume(m)

    # 5) Actor cues (starring / featuring / with actor / with <Name>).
    m = re.search(
        r"\b(?:starring|featuring|with\s+actor|cast\s+includes?)\s+"
        r"([A-Z][\w'\u2019.-]*(?:\s+[A-Z][\w'\u2019.-]*)*)",
        working,
    )
    if not m:
        # "with <Proper Name>" but not "with the character ..." (consumed).
        m = re.search(
            r"\bwith\s+(?!the\b|a\b|an\b)"
            r"([A-Z][\w'\u2019.-]*(?:\s+[A-Z][\w'\u2019.-]*)*)",
            working,
        )
    if m:
        f["actor"] = _clean_entity(m.group(1))
        consume(m)

    # 6) Season / episode numbers.
    m = re.search(r"\bseason\s+(\d{1,3})\b", working, re.I)
    if m:
        f["season"] = int(m.group(1))
        consume(m)
    m = re.search(r"\bepisode\s+(?:number\s+|#\s*)?(\d{1,3})\b", working, re.I)
    if m:
        f["episode"] = int(m.group(1))
        consume(m)

    # 7) Genre (explicit "genre X"/"category X" or a known genre word).
    m = re.search(r"\b(?:genre|category)\s+([A-Za-z]+)\b", working, re.I)
    if m and m.group(1).lower() in _GENRES:
        f["genre"] = _GENRES[m.group(1).lower()]
        consume(m)
    else:
        for word, canon in _GENRES.items():
            gm = re.search(rf"\b{word}\b", working, re.I)
            if gm:
                f["genre"] = canon
                consume(gm)
                break

    # 8) Channel ("on <Channel>" / "channel <X>").
    m = re.search(r"\bchannel\s+([A-Za-z0-9][\w.-]*)\b", working, re.I)
    if not m:
        m = re.search(r"\bon\s+([A-Z0-9][\w.-]*\d*[A-Za-z]*)\b", working)
    if m:
        cand = m.group(1)
        if cand.lower() not in ("the", "my", "dvr", "disk", "tv"):
            f["channel"] = cand
            consume(m)

    # 9) Episode title ("episode called/named X").
    m = re.search(
        r"\bepisode\s+(?:called|named|titled)\s+(.+?)(?:\?|$|\bfrom\b|\bon\b)",
        working, re.I,
    )
    if m:
        f["episode_title"] = _clean_entity(m.group(1))
        consume(m)

    # 10) Temporal expression (captured raw; resolved deterministically).
    m = _TEMPORAL_RE.search(working)
    if m and "original_air_date" not in f:
        f["recorded_between"] = DateRange(expression=_tidy(m.group(0)))
        consume(m)
    elif m and "original_air_date" in f:
        # A year already routed to original_air_date; drop temporal noise.
        consume(m)

    # 11) Watched state.
    if re.search(r"\bunwatched\b|\bnot\s+watched\b|\bhaven'?t\s+watched\b|"
                 r"\bnever\s+watched\b", working, re.I):
        f["watched"] = WatchedState.UNWATCHED
    elif re.search(r"\balready\s+watched\b|\bwatched\b", working, re.I):
        f["watched"] = WatchedState.WATCHED

    # 12) Archived.
    if re.search(r"\barchived\b|\bsaved\s+to\s+(?:disk|library)\b", working, re.I):
        f["archived"] = True

    # 12b) Future/scheduling-intent title ("is Shark Tank scheduled",
    #      "will Survivor record next week", "did NCIS record last night").
    #      Runs before the generic title cue so a scheduling question still
    #      binds its subject as the title filter.
    if "title" not in f and "episode_title" not in f:
        fm = _FUTURE_TITLE_RE.search(working)
        if fm:
            cand = _clean_title_candidate(fm.group(1))
            if cand:
                f["title"] = cand
                consume(fm)

    # 13) Title vs episode_title.
    if "title" not in f and "episode_title" not in f:
        title = _extract_title(working)
        if title:
            f["title"] = title
        else:
            # Bare "find X" with no other cue → episode_title (spec §4).
            em = re.search(r"\bfind\s+(.+?)(?:\?|$)", working, re.I)
            if em:
                cand = _clean_entity(em.group(1))
                if cand:
                    f["episode_title"] = cand

    if systems:
        # Dedupe preserving order.
        seen: list[MediaSystem] = []
        for s in systems:
            if s not in seen:
                seen.append(s)
        f["systems"] = seen

    # Character now routes to SageTV's cast-role filter, so no capability
    # clarification is needed here.

    return DetermineFilters(**f)


_TITLE_CUE_PATTERNS = [
    # "show me Law and Order from yesterday" / "show me Columbo"
    r"\bshow\s+me\s+(.+?)(?:\?|$)",
    # "show Columbo recordings" / "series Columbo" / "program Columbo"
    r"\b(?:show|series|program|programme)\s+(?:called\s+|named\s+|titled\s+)?(.+?)"
    r"\b(?:recordings?|episodes?)\b",
    r"\b(?:show|series|program|programme)\s+(?:called|named|titled)\s+(.+?)(?:\?|$)",
    # "recordings of Columbo" / "episodes of Columbo"
    r"\b(?:recordings?|episodes?)\s+of\s+(.+?)(?:\?|$)",
    # "Columbo recordings" / "Columbo episodes"
    r"\b(.+?)\s+(?:recordings?|episodes?)\b",
]
_TITLE_CUE_RE = [re.compile(p, re.I) for p in _TITLE_CUE_PATTERNS]

# Leading interrogative / inventory lead-ins that precede the real show name
# in phrasings like "what are all the <show> episodes", "how many <show>
# episodes do I have", "do I have any <show> episodes". Stripped iteratively.
# A lone leading "the"/"my" is deliberately NOT stripped so titles such as
# "The Voice" survive; only "all the"/"all my" chains are removed.
_PREAMBLE_STRIP = re.compile(
    r"^(?:"
    r"what(?:'s| is| are)?|which|how\s+many|"
    r"do\s+i\s+have(?:\s+any)?|have\s+i\s+got|i\s+have|i've\s+got|"
    r"all(?:\s+(?:the|my|of))?|any|some"
    r")\s+",
    re.I,
)


def _strip_inventory_preamble(cand: str) -> str:
    prev = None
    while prev != cand:
        prev = cand
        cand = _PREAMBLE_STRIP.sub("", cand).strip()
    return cand


_TITLE_REJECT = frozenset({
    "the", "my", "me", "a", "an", "all", "any", "some", "list",
    "what", "whats", "what's", "which", "who", "whose", "where",
    "when", "why", "how", "do", "does", "did", "have", "has",
    "i", "we", "you", "it", "that", "this", "show", "shows",
    "recording", "recordings", "episode", "episodes",
})

# Tokens that can never, on their own, constitute a real show title. Used to
# reject an all-filler capture (e.g. "going to", "it") so a case-insensitive
# scheduling cue doesn't bind a bogus title from a generic phrasing like
# "what shows are going to record over the next 7 days".
_TITLE_FILLER = _TITLE_REJECT | frozenset({
    "is", "are", "was", "were", "will", "going", "gonna", "to", "about",
    "set", "record", "records", "recorded", "scheduled", "schedule",
    "air", "airs", "airing", "aired", "over", "next", "coming", "about",
    "tonight", "today", "tomorrow", "anything", "something", "everything",
    "on", "for", "of", "and", "at", "in", "going to", "gonna",
})


def _clean_title_candidate(cand: str | None) -> str | None:
    """Normalize a raw title capture and reject bare interrogatives/fillers."""
    if cand:
        cand = _clean_entity(cand)
    if cand:
        cand = _strip_inventory_preamble(cand)
    if not cand or cand.lower() in _TITLE_REJECT:
        return None
    # Drop a leading stray verb like "find"/"list"/"get".
    cand = re.sub(
        r"^(?:find|list|get|see|view|display)\s+", "", cand, flags=re.I
    ).strip()
    cand = _clean_entity(cand)
    if not cand:
        return None
    # Reject a capture made entirely of filler/stopwords (e.g. "going to").
    _toks = [t for t in re.split(r"\s+", cand.lower()) if t]
    if _toks and all(t in _TITLE_FILLER for t in _toks):
        return None
    return cand


# Future/scheduling-intent title cue: the subject that precedes a scheduling
# verb, e.g. "is Shark Tank scheduled", "will survivor record next week",
# "did NCIS record last night". Case-insensitive; an all-filler capture (e.g.
# "what shows are going to record ...") is rejected by _clean_title_candidate.
_FT_WORD = r"[A-Za-z0-9][\w'\u2019.&:-]*"
_FUTURE_TITLE_RE = re.compile(
    r"\b(?:is|are|will|was|were|does|do|did|"
    r"when\s+(?:is|are|will|does|do|did))\s+"
    rf"({_FT_WORD}(?:\s+(?:and|of|the|&)\s+{_FT_WORD}|\s+{_FT_WORD})*?)\s+"
    r"(?:scheduled|going\s+to\s+record|gonna\s+record|about\s+to\s+record|"
    r"set\s+to\s+record|record(?:ing|s|ed)?|air(?:ing|s|ed)?)\b",
    re.I,
)


def _extract_title(working: str) -> str | None:
    for rx in _TITLE_CUE_RE:
        m = rx.search(working)
        if m:
            cand = _clean_title_candidate(m.group(1))
            if cand:
                return cand
    return None


def filters_from_partial(raw: dict, prompt: str | None = None) -> DetermineFilters:
    """Validate/coerce an LLM-produced partial into ``DetermineFilters``.

    Any planner-supplied date timestamps are ignored; only the expression is
    kept and re-resolved deterministically (the planner must not compute time).
    """
    data = dict(raw or {})
    rb = data.get("recorded_between")
    if isinstance(rb, dict):
        data["recorded_between"] = DateRange(expression=rb.get("expression"))
    elif isinstance(rb, str):
        data["recorded_between"] = DateRange(expression=rb)
    return DetermineFilters(**data)
