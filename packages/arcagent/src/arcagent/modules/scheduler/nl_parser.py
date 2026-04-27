"""Natural-language cron parser — SPEC-018 T1.11.

Deterministic-first pipeline:
  parse_interval  →  parse_duration_oneshot  →  parse_cron_cronsim  →  parse_iso_dateparser
  → optional LLM fallback (disabled at federal tier).

Federal tier: deterministic-only; raises ParseError on any unknown input.
Enterprise / personal: LLM fallback via arcllm tool-use with strict JSON schema.

DST correctness: cronsim (Healthchecks.io) replaces croniter throughout.
Croniter has known DST evaluation bugs (fires wrong wall-clock after spring-forward).
cronsim iterates zoneinfo-aware datetime objects so transitions are correct.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim
from dateparser import parse as dateparser_parse
from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    pass

_logger = logging.getLogger("arcagent.scheduler.nl_parser")

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class ParseError(ValueError):
    """Raised when no deterministic parser can handle the input."""


# ---------------------------------------------------------------------------
# Schedule model
# ---------------------------------------------------------------------------

#: Maximum gap between consecutive fires before sanity validation rejects.
_MAX_GAP_DAYS = 366

#: Minimum interval between consecutive fires (prevents every-second abuse).
_MIN_INTERVAL_SECONDS = 60

#: Number of fire times computed for sanity validation.
_SANITY_FIRE_COUNT = 5

#: Regex pattern for valid IANA timezone in LLM output.
_TZ_PATTERN = re.compile(r"^[A-Za-z_]+/[A-Za-z_]+$")


class Schedule(BaseModel):
    """Normalised schedule — the output of every parse path."""

    kind: str  # "cron" | "interval" | "once"
    cron_or_iso: str | None = None
    interval_minutes: int | None = None
    tz: str
    next_fires: list[datetime] = []

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        if v not in {"cron", "interval", "once"}:
            msg = f"kind must be 'cron', 'interval', or 'once'; got '{v}'"
            raise ValueError(msg)
        return v

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError) as exc:
            msg = f"Unknown timezone '{v}'"
            raise ValueError(msg) from exc
        return v


# ---------------------------------------------------------------------------
# Interval patterns  ("30m", "2h", "1d", "every 30m", …)
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(
    r"^(?:every\s+)?(\d+)\s*(s(?:ec(?:onds?)?)?|m(?:in(?:utes?)?)?|h(?:ours?)?|d(?:ays?)?)$",
    re.IGNORECASE,
)

_UNIT_TO_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def _unit_seconds(unit_str: str) -> int:
    """Map a unit abbreviation (first letter) to seconds."""
    return _UNIT_TO_SECONDS[unit_str[0].lower()]


def parse_interval(text: str, user_tz: str) -> Schedule:
    """Parse "every 30m", "2h", "1d", etc. into an interval Schedule.

    Raises ParseError if the text does not match the interval pattern.
    """
    m = _INTERVAL_RE.match(text.strip())
    if not m:
        raise ParseError(f"Not an interval expression: '{text}'")
    amount = int(m.group(1))
    seconds = amount * _unit_seconds(m.group(2))
    minutes = max(1, seconds // 60)
    return Schedule(
        kind="interval",
        interval_minutes=minutes,
        tz=user_tz,
    )


# ---------------------------------------------------------------------------
# One-shot via dateparser  ("in 2 hours", "tomorrow 9am", ISO timestamps)
# ---------------------------------------------------------------------------

# Patterns that look like relative durations for one-shot scheduling.
# "30m", "2h", "1d" with optional trailing s are matched by parse_interval first;
# this branch captures "in 30 minutes", "in 2 hours", etc.
_ONESHOT_KEYWORDS = re.compile(
    r"\bin\s+\d|\btomorrow\b|\bnext\b|\btoday\b|\bmonday\b|\btuesday\b"
    r"|\bwednesday\b|\bthursday\b|\bfriday\b|\bsaturday\b|\bsunday\b"
    r"|\bat\s+\d|\bam\b|\bpm\b",
    re.IGNORECASE,
)

_ISO_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_duration_oneshot(text: str, user_tz: str) -> Schedule:
    """Parse one-shot NL durations: "in 2 hours", "tomorrow 9am", …

    Uses dateparser with the user's timezone.
    Raises ParseError if no date is found or it's in the past.
    """
    stripped = text.strip()
    # Only attempt if the text looks like a relative/day-of-week expression
    # (ISO timestamps are handled by parse_iso_dateparser).
    if not (_ONESHOT_KEYWORDS.search(stripped) or _ISO_LIKE.search(stripped)):
        raise ParseError(f"Not a recognizable one-shot expression: '{text}'")

    tz = ZoneInfo(user_tz)
    settings: dict[str, Any] = {
        "TIMEZONE": user_tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    result = dateparser_parse(stripped, settings=settings)
    if result is None:
        raise ParseError(f"dateparser could not parse: '{text}'")

    # Ensure the result is tz-aware.
    if result.tzinfo is None:
        result = result.replace(tzinfo=tz)

    now = datetime.now(tz=UTC)
    if result < now:
        raise ParseError(f"Parsed date '{result}' is in the past")

    return Schedule(
        kind="once",
        cron_or_iso=result.isoformat(),
        tz=user_tz,
    )


# ---------------------------------------------------------------------------
# Cron expression via cronsim
# ---------------------------------------------------------------------------

# Rough pre-check: 5-field cron (ignoring L/W/#) — cronsim validates precisely.
# Month names (JAN-DEC) and day names (MON-SUN) are listed as alternations
# inside non-capturing groups to avoid invalid character-range errors.
_CRON_RE = re.compile(
    r"^(\*|[0-9,\-/]+)\s+"  # minute
    r"(\*|[0-9,\-/]+)\s+"  # hour
    r"(\*|[0-9,\-/?L]+)\s+"  # day-of-month
    r"(\*|[0-9,\-/]|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC|[a-z])+\s+"  # month
    r"(\*|[0-9,\-/]|MON|TUE|WED|THU|FRI|SAT|SUN|[a-z])+$",  # day-of-week
    re.IGNORECASE,
)


def parse_cron_cronsim(text: str, user_tz: str) -> Schedule:
    """Parse a 5-field cron expression using cronsim.

    cronsim is DST-correct (iterates zoneinfo-aware datetimes).
    Raises ParseError if the expression is invalid or not a cron string.
    """
    stripped = text.strip()
    if not _CRON_RE.match(stripped):
        raise ParseError(f"Not a cron expression: '{text}'")
    tz = ZoneInfo(user_tz)
    # Probe cronsim to validate the expression.
    try:
        probe = CronSim(stripped, datetime.now(tz=tz))
        next(probe)
    except (ValueError, StopIteration) as exc:
        raise ParseError(f"cronsim rejected expression '{text}': {exc}") from exc
    except Exception as exc:
        raise ParseError(f"cronsim error for '{text}': {exc}") from exc

    return Schedule(
        kind="cron",
        cron_or_iso=stripped,
        tz=user_tz,
    )


# ---------------------------------------------------------------------------
# ISO 8601 timestamp via dateparser
# ---------------------------------------------------------------------------


def parse_iso_dateparser(text: str, user_tz: str) -> Schedule:
    """Parse ISO 8601 timestamps: "2026-05-01T09:00:00-05:00".

    Raises ParseError if dateparser cannot interpret the text as a date.
    """
    stripped = text.strip()
    # Only attempt if it looks like an absolute date/time.
    if not _ISO_LIKE.search(stripped):
        raise ParseError(f"Not an ISO-like timestamp: '{text}'")

    settings: dict[str, Any] = {
        "TIMEZONE": user_tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    result = dateparser_parse(stripped, settings=settings)
    if result is None:
        raise ParseError(f"dateparser could not parse ISO timestamp: '{text}'")

    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo(user_tz))

    return Schedule(
        kind="once",
        cron_or_iso=result.isoformat(),
        tz=user_tz,
    )


# ---------------------------------------------------------------------------
# Sanity validator
# ---------------------------------------------------------------------------


def _next_fires_cron(expr: str, tz: ZoneInfo, count: int) -> list[datetime]:
    """Compute next N fire times for a cron expression."""
    now = datetime.now(tz=tz)
    sim = CronSim(expr, now)
    fires: list[datetime] = []
    for _ in range(count):
        try:
            fires.append(next(sim))
        except StopIteration:
            break
    return fires


def _next_fires_interval(
    interval_minutes: int,
    count: int,
) -> list[datetime]:
    """Compute next N fire times for an interval schedule."""
    now = datetime.now(tz=UTC)
    return [now + timedelta(minutes=interval_minutes * i) for i in range(1, count + 1)]


def _next_fires_once(iso: str, count: int) -> list[datetime]:
    """Return the single fire time for a one-shot schedule."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return [dt] if count >= 1 else []


def validate(schedule: Schedule) -> Schedule:
    """Sanity-validate a Schedule by computing next 5 fire times.

    Rejects if:
    - fewer than 1 fire time computed
    - min gap < 60 seconds (prevents every-second abuse — LLM10)
    - max gap > 366 days (dead schedule detection)
    - iteration raises an exception

    Returns the schedule with `next_fires` populated.
    """
    tz = ZoneInfo(schedule.tz)

    try:
        if schedule.kind == "cron":
            if not schedule.cron_or_iso:
                raise ParseError("Cron schedule missing expression")
            fires = _next_fires_cron(schedule.cron_or_iso, tz, _SANITY_FIRE_COUNT)
        elif schedule.kind == "interval":
            if not schedule.interval_minutes:
                raise ParseError("Interval schedule missing interval_minutes")
            fires = _next_fires_interval(schedule.interval_minutes, _SANITY_FIRE_COUNT)
        else:
            if not schedule.cron_or_iso:
                raise ParseError("Once schedule missing cron_or_iso")
            fires = _next_fires_once(schedule.cron_or_iso, _SANITY_FIRE_COUNT)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"Error computing fire times: {exc}") from exc

    if not fires:
        raise ParseError("Schedule produces no future fire times (dead schedule)")

    # Check minimum interval between consecutive fires.
    for i in range(1, len(fires)):
        gap_seconds = (fires[i] - fires[i - 1]).total_seconds()
        if gap_seconds < _MIN_INTERVAL_SECONDS:
            raise ParseError(
                f"Consecutive fires {gap_seconds:.1f}s apart — below minimum "
                f"{_MIN_INTERVAL_SECONDS}s (LLM10: unbounded consumption guard)"
            )

    # Check maximum gap (dead schedule guard).
    if len(fires) >= 2:
        max_gap_seconds = max(
            (fires[i] - fires[i - 1]).total_seconds() for i in range(1, len(fires))
        )
        if max_gap_seconds > _MAX_GAP_DAYS * 86400:
            raise ParseError(
                f"Maximum gap {max_gap_seconds / 86400:.1f} days exceeds "
                f"{_MAX_GAP_DAYS} day limit"
            )

    return schedule.model_copy(update={"next_fires": fires})


# ---------------------------------------------------------------------------
# LLM fallback schema
# ---------------------------------------------------------------------------

_NORMALIZE_SCHEDULE_TOOL: dict[str, Any] = {
    "name": "normalize_schedule",
    "description": (
        "Convert a natural-language schedule description into a structured "
        "schedule definition.  Return exactly one JSON object."
    ),
    "input_schema": {
        "type": "object",
        "required": ["kind", "cron_or_iso", "tz"],
        "properties": {
            "kind": {"enum": ["cron", "interval", "once"]},
            "cron_or_iso": {"type": "string"},
            "interval_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 525600,
            },
            "tz": {
                "type": "string",
                "pattern": r"^[A-Za-z_]+/[A-Za-z_]+$",
            },
            "rationale": {
                "type": "string",
                "maxLength": 200,
            },
        },
    },
}


def _reify(raw: dict[str, Any], user_tz: str) -> Schedule:
    """Convert LLM tool-call arguments into a Schedule.

    Validates required fields and TZ pattern before constructing the model.
    """
    kind = raw.get("kind")
    if kind not in {"cron", "interval", "once"}:
        raise ParseError(f"LLM returned invalid kind: '{kind}'")

    tz = raw.get("tz", user_tz)
    if not _TZ_PATTERN.match(str(tz)):
        _logger.warning("LLM returned non-IANA tz '%s'; falling back to user_tz", tz)
        tz = user_tz

    cron_or_iso = raw.get("cron_or_iso") or None
    interval_minutes = raw.get("interval_minutes")

    return Schedule(
        kind=kind,
        cron_or_iso=cron_or_iso,
        interval_minutes=interval_minutes,
        tz=tz,
    )


async def _call_llm_fallback(
    text: str,
    user_tz: str,
    model_id: str = "anthropic",
) -> dict[str, Any]:
    """Invoke arcllm tool-use to normalize a schedule string.

    Uses arcllm's load_model (auxiliary client pattern).
    Returns the raw arguments dict from the tool call.
    Raises ParseError on any failure.

    Args:
        text: Natural-language schedule string to normalize.
        user_tz: IANA timezone name.
        model_id: ``provider`` or ``provider/model`` string passed to
            ``arcllm.load_model()``. Defaults to ``"anthropic"`` but can
            be overridden via ``ARCAGENT_SCHEDULER_LLM`` env variable or
            by callers that have the agent config available.
    """
    import os

    # Allow operator override via environment variable.
    model_id = os.environ.get("ARCAGENT_SCHEDULER_LLM", model_id)

    # Import here to avoid hard dep at module init;
    # federal tier never reaches this path.
    try:
        from arcllm import LLMProvider, Message, Tool, load_model
    except ImportError as exc:
        raise ParseError(f"arcllm not available for LLM fallback: {exc}") from exc

    provider, _, model_name = model_id.partition("/")
    model: LLMProvider = load_model(provider, model_name or None)
    tool = Tool(
        name=_NORMALIZE_SCHEDULE_TOOL["name"],
        description=_NORMALIZE_SCHEDULE_TOOL["description"],
        parameters=_NORMALIZE_SCHEDULE_TOOL["input_schema"],
    )
    messages = [
        Message(
            role="user",
            content=(
                f"Convert this to a schedule: \"{text}\"\n"
                f"User timezone: {user_tz}\n"
                "Use the normalize_schedule tool to return the result."
            ),
        )
    ]
    try:
        response = await model.invoke(messages, tools=[tool])
    except Exception as exc:
        raise ParseError(f"LLM fallback invocation failed: {exc}") from exc

    if not response.tool_calls:
        raise ParseError("LLM fallback returned no tool calls")

    for tc in response.tool_calls:
        if tc.name == "normalize_schedule":
            return tc.arguments

    raise ParseError("LLM fallback did not call normalize_schedule")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def parse(text: str, user_tz: str, federal: bool | Any) -> Schedule:
    """Parse a natural-language or cron/ISO schedule string.

    Pipeline (deterministic-first per SDD §3.4):
      1. parse_interval          — "30m", "2h", "every 1d"
      2. parse_duration_oneshot  — "in 2 hours", "tomorrow 9am"
      3. parse_cron_cronsim      — "0 9 * * 1-5"
      4. parse_iso_dateparser    — "2026-05-01T09:00:00-05:00"
      LLM fallback (federal=False only)

    Federal tier disables LLM fallback: deterministic-only mode.

    Args:
        text: Schedule string to parse.
        user_tz: IANA timezone name (e.g., "America/New_York").
        federal: Either a ``bool`` (legacy API) or a ``PolicyContext`` object
            with an ``is_federal`` property (new API). Federal mode disables
            LLM fallback and raises ParseError on unknown input.
    """
    # Resolve federal flag from either bool or PolicyContext
    is_fed: bool = federal.is_federal if hasattr(federal, "is_federal") else bool(federal)

    _logger.debug("Parsing schedule '%s' (tz=%s, federal=%s)", text, user_tz, is_fed)

    for fn in (
        parse_interval,
        parse_duration_oneshot,
        parse_cron_cronsim,
        parse_iso_dateparser,
    ):
        try:
            return validate(fn(text, user_tz))
        except ParseError:
            continue

    if is_fed:
        raise ParseError(
            "deterministic-only mode: unrecognized schedule — "
            "use cron syntax, ISO 8601, or an interval like '30m'"
        )

    # LLM fallback — personal / enterprise tier only.
    _logger.info("Deterministic parsers failed; invoking LLM fallback for '%s'", text)
    raw = await _call_llm_fallback(text, user_tz)
    return validate(_reify(raw, user_tz))


def parse_sync(text: str, user_tz: str, *, federal: bool = False) -> Schedule:
    """Synchronous thin wrapper for contexts that cannot await.

    Not suitable for production async paths; used only in CLI / tests.
    Runs the async pipeline in a new event loop.
    """
    import asyncio

    return asyncio.run(parse(text, user_tz, federal))
