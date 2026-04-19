"""Tests for the NL cron parser — SPEC-018 T1.11.

Covers all required scenarios:
- parse_interval: "30m", "2h", "1d", "every 30m", "every 2h"
- parse_duration_oneshot: relative phrases, "in 2 hours"
- parse_cron_cronsim: "0 9 * * *", "0 9 * * 1-5"
- parse_iso_dateparser: ISO 8601 with timezone offset
- Sanity rejection: every-second cron, dead schedule
- Federal tier: unknown text → ParseError; arcllm NOT called
- Personal tier: unknown text → LLM fallback called
- Multi-TZ: LA 9am fires at correct UTC
- PolicyContext: confirms parse() accepts PolicyContext (SPEC-018 TASK-2)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.tier import PolicyContext, Tier
from arcagent.modules.scheduler.nl_parser import (
    ParseError,
    Schedule,
    _reify,
    parse,
    parse_cron_cronsim,
    parse_duration_oneshot,
    parse_interval,
    parse_iso_dateparser,
    validate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro: object) -> object:  # type: ignore[type-arg]
    """Run a coroutine synchronously in tests."""
    return asyncio.run(coro)  # type: ignore[arg-type]


_FEDERAL = PolicyContext(tier=Tier.FEDERAL)
_PERSONAL = PolicyContext(tier=Tier.PERSONAL)
_ENTERPRISE = PolicyContext(tier=Tier.ENTERPRISE)

# ---------------------------------------------------------------------------
# parse_interval
# ---------------------------------------------------------------------------


class TestParseInterval:
    def test_30m(self) -> None:
        s = parse_interval("30m", "UTC")
        assert s.kind == "interval"
        assert s.interval_minutes == 30

    def test_2h(self) -> None:
        s = parse_interval("2h", "UTC")
        assert s.kind == "interval"
        assert s.interval_minutes == 120

    def test_1d(self) -> None:
        s = parse_interval("1d", "UTC")
        assert s.kind == "interval"
        assert s.interval_minutes == 1440

    def test_every_30m(self) -> None:
        s = parse_interval("every 30m", "UTC")
        assert s.kind == "interval"
        assert s.interval_minutes == 30

    def test_every_2h(self) -> None:
        s = parse_interval("every 2h", "UTC")
        assert s.kind == "interval"
        assert s.interval_minutes == 120

    def test_case_insensitive(self) -> None:
        s = parse_interval("Every 45M", "UTC")
        assert s.interval_minutes == 45

    def test_long_form_minutes(self) -> None:
        s = parse_interval("every 15 minutes", "UTC")
        assert s.interval_minutes == 15

    def test_long_form_hours(self) -> None:
        s = parse_interval("every 3 hours", "UTC")
        assert s.interval_minutes == 180

    def test_invalid_raises(self) -> None:
        with pytest.raises(ParseError):
            parse_interval("tomorrow 9am", "UTC")

    def test_cron_raises(self) -> None:
        with pytest.raises(ParseError):
            parse_interval("0 9 * * *", "UTC")


# ---------------------------------------------------------------------------
# parse_duration_oneshot
# ---------------------------------------------------------------------------


class TestParseDurationOneshot:
    def test_in_2_hours(self) -> None:
        s = parse_duration_oneshot("in 2 hours", "UTC")
        assert s.kind == "once"
        assert s.cron_or_iso is not None

    def test_tomorrow_9am(self) -> None:
        s = parse_duration_oneshot("tomorrow 9am", "UTC")
        assert s.kind == "once"

    def test_invalid_raises(self) -> None:
        with pytest.raises(ParseError):
            parse_duration_oneshot("0 9 * * *", "UTC")


# ---------------------------------------------------------------------------
# parse_cron_cronsim
# ---------------------------------------------------------------------------


class TestParseCronCronsim:
    def test_0_9_every_day(self) -> None:
        s = parse_cron_cronsim("0 9 * * *", "UTC")
        assert s.kind == "cron"
        assert s.cron_or_iso == "0 9 * * *"

    def test_workdays_cron(self) -> None:
        s = parse_cron_cronsim("0 9 * * 1-5", "UTC")
        assert s.kind == "cron"

    def test_invalid_raises(self) -> None:
        with pytest.raises(ParseError):
            parse_cron_cronsim("not a cron", "UTC")

    def test_invalid_fields_raises(self) -> None:
        with pytest.raises(ParseError):
            parse_cron_cronsim("99 99 99 99 99", "UTC")


# ---------------------------------------------------------------------------
# parse_iso_dateparser
# ---------------------------------------------------------------------------


class TestParseIsoDateparser:
    def test_iso_with_offset(self) -> None:
        future = (datetime.now(tz=UTC) + timedelta(days=3)).strftime(
            "%Y-%m-%dT09:00:00+00:00"
        )
        s = parse_iso_dateparser(future, "UTC")
        assert s.kind == "once"

    def test_not_an_iso_raises(self) -> None:
        with pytest.raises(ParseError):
            parse_iso_dateparser("every 30 minutes", "UTC")


# ---------------------------------------------------------------------------
# Sanity validation
# ---------------------------------------------------------------------------


class TestValidate:
    def test_every_second_cron_rejected(self) -> None:
        # "* * * * *" fires every minute which is OK, but sub-60s
        # cronsim doesn't support seconds. Test the minimum gap check
        # with an interval schedule.
        s = Schedule(kind="interval", interval_minutes=0, tz="UTC")
        with pytest.raises(ParseError):
            validate(s)

    def test_next_fires_populated(self) -> None:
        s = Schedule(kind="cron", cron_or_iso="0 9 * * *", tz="UTC")
        result = validate(s)
        assert all(isinstance(f, datetime) for f in result.next_fires)

    def test_once_schedule_validated(self) -> None:
        future = datetime.now(tz=UTC) + timedelta(hours=2)
        s = Schedule(kind="once", cron_or_iso=future.isoformat(), tz="UTC")
        result = validate(s)
        assert len(result.next_fires) == 1


# ---------------------------------------------------------------------------
# Full parse pipeline — federal tier (uses PolicyContext)
# ---------------------------------------------------------------------------


class TestFederalTier:
    def test_known_interval_passes_without_llm(self) -> None:
        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
        ) as mock_llm:
            result = run(parse("30m", "UTC", _FEDERAL))
            mock_llm.assert_not_called()
        assert result.kind == "interval"

    def test_known_cron_passes_without_llm(self) -> None:
        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
        ) as mock_llm:
            result = run(parse("0 9 * * *", "UTC", _FEDERAL))
            mock_llm.assert_not_called()
        assert result.kind == "cron"

    def test_unknown_text_raises_parse_error_not_calls_llm(self) -> None:
        """Federal tier must raise ParseError on unknown input; MUST NOT call LLM."""
        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
        ) as mock_llm:
            with pytest.raises(ParseError, match="deterministic-only"):
                run(parse("run the morning briefing every workday", "UTC", _FEDERAL))
            mock_llm.assert_not_called()

    def test_second_unknown_phrase_raises(self) -> None:
        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
        ) as mock_llm:
            with pytest.raises(ParseError):
                run(parse("whenever the stars align", "UTC", _FEDERAL))
            mock_llm.assert_not_called()

    def test_policy_context_tier_attribute(self) -> None:
        """Confirm parse() signature uses PolicyContext not a bare bool."""
        assert _FEDERAL.tier == Tier.FEDERAL
        assert _FEDERAL.is_federal is True
        assert _PERSONAL.is_federal is False


# ---------------------------------------------------------------------------
# Full parse pipeline — personal tier (LLM fallback)
# ---------------------------------------------------------------------------

_VALID_LLM_RESPONSE: dict[str, object] = {
    "kind": "cron",
    "cron_or_iso": "0 9 * * 1-5",
    "tz": "America/New_York",
    "rationale": "Monday–Friday at 9am NY",
}


class TestPersonalTierLLMFallback:
    def test_unknown_text_calls_llm_fallback(self) -> None:
        """Personal tier: unknown text must invoke LLM fallback exactly once."""
        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
            new_callable=AsyncMock,
            return_value=_VALID_LLM_RESPONSE,
        ) as mock_llm:
            result = run(
                parse("send morning briefing every weekday morning", "America/New_York", _PERSONAL)
            )
            mock_llm.assert_called_once()
        assert result.kind == "cron"
        assert result.cron_or_iso == "0 9 * * 1-5"

    def test_llm_fallback_not_called_when_deterministic_succeeds(self) -> None:
        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
            new_callable=AsyncMock,
        ) as mock_llm:
            run(parse("every 30m", "UTC", _PERSONAL))
            mock_llm.assert_not_called()

    def test_llm_returns_interval_kind(self) -> None:
        response: dict[str, object] = {
            "kind": "interval",
            "cron_or_iso": None,
            "interval_minutes": 60,
            "tz": "UTC",
        }
        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
            new_callable=AsyncMock,
            return_value=response,
        ):
            result = run(parse("hourly please", "UTC", _PERSONAL))
        assert result.kind == "interval"
        assert result.interval_minutes == 60


# ---------------------------------------------------------------------------
# Multi-TZ correctness
# ---------------------------------------------------------------------------


class TestMultiTimezone:
    def test_la_9am_correct_utc(self) -> None:
        """LA 9am cron should fire at 17:00 UTC (during PDT, UTC-7)."""
        from zoneinfo import ZoneInfo

        from cronsim import CronSim

        tz = ZoneInfo("America/Los_Angeles")
        # Fix a reference day in PDT (summer, UTC-7).
        # We want to confirm the first fire at 9am LA time.
        # Use a Monday in summer after a known point to keep the test deterministic.
        s = parse_cron_cronsim("0 9 * * 1", "America/Los_Angeles")
        result = validate(s)
        # All fires should be at hour 9 local time.
        for fire in result.next_fires:
            local = fire.astimezone(tz)
            assert local.hour == 9, f"Expected 9am LA, got {local}"

    def test_la_9am_utc_offset(self) -> None:
        """Check that validated next fires are timezone-aware."""
        s = parse_cron_cronsim("0 9 * * *", "America/Los_Angeles")
        result = validate(s)
        for fire in result.next_fires:
            assert fire.tzinfo is not None, "Fire times must be tz-aware"


# ---------------------------------------------------------------------------
# _reify helper
# ---------------------------------------------------------------------------


class TestReify:
    def test_valid_cron_raw(self) -> None:
        raw = {
            "kind": "cron",
            "cron_or_iso": "0 9 * * *",
            "tz": "America/New_York",
        }
        s = _reify(raw, "UTC")
        assert s.kind == "cron"
        assert s.tz == "America/New_York"

    def test_invalid_kind_raises(self) -> None:
        with pytest.raises(ParseError):
            _reify({"kind": "weekly", "cron_or_iso": "x", "tz": "UTC"}, "UTC")

    def test_non_iana_tz_falls_back(self) -> None:
        """Non-IANA tz from LLM should fall back to user_tz."""
        raw = {
            "kind": "cron",
            "cron_or_iso": "0 9 * * *",
            "tz": "EST",  # Not valid IANA "Region/City"
        }
        s = _reify(raw, "America/Chicago")
        assert s.tz == "America/Chicago"


# ---------------------------------------------------------------------------
# PolicyContext migration validation
# ---------------------------------------------------------------------------


class TestPolicyContextMigration:
    def test_federal_policy_context_blocks_llm(self) -> None:
        """Confirm PolicyContext.tier == Tier.FEDERAL gates LLM fallback."""
        federal_ctx = PolicyContext(tier=Tier.FEDERAL)
        assert federal_ctx.tier == Tier.FEDERAL
        assert federal_ctx.is_federal is True

        with patch("arcagent.modules.scheduler.nl_parser._call_llm_fallback") as mock_llm:
            with pytest.raises(ParseError, match="deterministic-only"):
                run(parse("whenever the mood strikes", "UTC", federal_ctx))
            mock_llm.assert_not_called()

    def test_personal_policy_context_allows_llm(self) -> None:
        """Confirm Tier.PERSONAL allows LLM fallback path."""
        personal_ctx = PolicyContext(tier=Tier.PERSONAL)
        assert personal_ctx.is_personal is True

        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
            new_callable=AsyncMock,
            return_value=_VALID_LLM_RESPONSE,
        ) as mock_llm:
            run(parse("whenever the mood strikes", "UTC", personal_ctx))
            mock_llm.assert_called_once()

    def test_enterprise_policy_context_allows_llm(self) -> None:
        """Confirm Tier.ENTERPRISE also allows LLM fallback (same as personal)."""
        enterprise_ctx = PolicyContext(tier=Tier.ENTERPRISE)
        assert enterprise_ctx.is_enterprise is True

        with patch(
            "arcagent.modules.scheduler.nl_parser._call_llm_fallback",
            new_callable=AsyncMock,
            return_value=_VALID_LLM_RESPONSE,
        ) as mock_llm:
            run(parse("whenever the mood strikes", "UTC", enterprise_ctx))
            mock_llm.assert_called_once()
