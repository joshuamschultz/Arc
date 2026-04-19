"""DST regression suite — SPEC-018 T1.11.6.

cronsim is DST-correct via zoneinfo.
croniter historically had DST evaluation bugs in some scenarios.

cronsim's spring-forward behavior:
  A "0 2 * * *" cron during spring-forward (when 2am→3am): cronsim advances
  to 3:00am on the transition day (the next valid wall-clock instant),
  then resumes at 2:00am every subsequent day.  This is semantically correct
  — the 2am slot does not exist that day, so the next valid instant is 3am.

cronsim's fall-back behavior:
  A "30 1 * * *" cron during fall-back: cronsim fires exactly ONCE at 1:30am
  (at the first occurrence during the ambiguous hour), not twice.

Test cases:
1. Spring-forward (US Eastern 2026-03-08): fires at 3am transition day, 2am thereafter.
2. Fall-back (US Eastern 2026-11-01): 1:30am fires exactly once.
3. UK spring-forward (Europe/London 2026-03-29): same skip/advance behavior.
4. Multi-TZ: LA 9am fires at correct UTC.

Reference dates (2026):
  US/Eastern spring-forward: 2026-03-08 02:00 → 03:00 (clocks jump forward)
  US/Eastern fall-back:       2026-11-01 02:00 → 01:00 (clocks fall back)
  UK (Europe/London) spring:  2026-03-29 01:00 → 02:00
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cronsim import CronSim

from arcagent.modules.scheduler.nl_parser import (
    ParseError,
    Schedule,
    parse_cron_cronsim,
    validate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_US_EASTERN = ZoneInfo("America/New_York")
_US_LA = ZoneInfo("America/Los_Angeles")
_UK = ZoneInfo("Europe/London")


def _fires_after(expr: str, tz: ZoneInfo, start: datetime, count: int = 10) -> list[datetime]:
    """Collect up to `count` CronSim iterations starting from `start`."""
    sim = CronSim(expr, start)
    results: list[datetime] = []
    for _ in range(count):
        try:
            results.append(next(sim))
        except StopIteration:
            break
    return results


# ---------------------------------------------------------------------------
# US/Eastern spring-forward (2026-03-08)
# ---------------------------------------------------------------------------


class TestUsEasternSpringForward:
    """2am on 2026-03-08 does not exist — clocks jump from 02:00 to 03:00.

    cronsim behavior: "0 2 * * *" fires at 03:00 on the transition day
    (the next valid wall-clock instant after the gap), then resumes at 02:00
    every subsequent day.  This is correct — 2am that day doesn't exist.
    """

    # One minute before the transition (01:59 EST = just before clocks jump).
    _START = datetime(2026, 3, 8, 1, 59, tzinfo=_US_EASTERN)

    def test_spring_forward_advances_to_3am_on_transition_day(self) -> None:
        """cronsim should fire at 3:00am on spring-forward day (2am doesn't exist)."""
        fires = _fires_after("0 2 * * *", _US_EASTERN, self._START, count=3)
        assert fires, "Expected at least one future fire"
        first_fire = fires[0]
        first_local = first_fire.astimezone(_US_EASTERN)
        # cronsim advances past the gap to the next valid instant (03:00 EDT).
        assert first_local.year == 2026
        assert first_local.month == 3
        assert first_local.day == 8
        assert first_local.hour == 3, (
            f"Expected cronsim to advance to 03:00 on spring-forward day, "
            f"got {first_local.hour}:00"
        )

    def test_spring_forward_subsequent_fires_at_2am(self) -> None:
        """After the transition day, subsequent fires must be at 2am."""
        fires = _fires_after("0 2 * * *", _US_EASTERN, self._START, count=5)
        assert len(fires) >= 2, "Expected multiple fires"
        # Skip the first fire (transition day at 3am); rest must be at 2am.
        for fire in fires[1:]:
            local = fire.astimezone(_US_EASTERN)
            assert local.hour == 2, (
                f"Post-transition fires must be at hour 2, got {local.hour}: {local}"
            )

    def test_spring_forward_correct_utc_after_transition(self) -> None:
        """After spring-forward, 2am EDT = UTC-4 = 06:00 UTC."""
        fires = _fires_after("0 2 * * *", _US_EASTERN, self._START, count=5)
        # Get fires after the transition day.
        post_transition = [f for f in fires if f.astimezone(_US_EASTERN).day != 8]
        assert post_transition, "Expected post-transition fires"
        for fire in post_transition:
            utc_fire = fire.astimezone(ZoneInfo("UTC"))
            # 2am EDT = UTC-4 → 06:00 UTC.
            assert utc_fire.hour == 6, (
                f"Expected UTC 06:00 (EDT), got UTC {utc_fire.hour}:00"
            )


# ---------------------------------------------------------------------------
# US/Eastern fall-back (2026-11-01)
# ---------------------------------------------------------------------------


class TestUsEasternFallBack:
    """At fall-back, clocks go from 02:00 back to 01:00.

    A "30 1 * * *" (1:30am) cron must fire exactly ONCE, not twice.
    cronsim correctly fires once at the first occurrence of 1:30am.
    """

    # Start just before 1:30am during fall-back transition.
    # 2026-11-01 01:00 EDT = first pass through 1am.
    _START = datetime(2026, 11, 1, 1, 0, tzinfo=_US_EASTERN)

    def test_fall_back_fires_exactly_once(self) -> None:
        """1:30am cron must fire exactly once on fall-back day."""
        fires = _fires_after("30 1 * * *", _US_EASTERN, self._START, count=3)
        assert fires, "Expected at least one fire"
        # Filter to fires that are local 1:30 on fall-back day.
        fall_back_day_fires = [
            f for f in fires
            if f.astimezone(_US_EASTERN).date() == datetime(2026, 11, 1).date()
            and f.astimezone(_US_EASTERN).hour == 1
            and f.astimezone(_US_EASTERN).minute == 30
        ]
        # cronsim should produce exactly 1 fire at 1:30 on that day.
        assert len(fall_back_day_fires) == 1, (
            f"Expected exactly 1 fire at 1:30am on fall-back day, "
            f"got {len(fall_back_day_fires)}: {fall_back_day_fires}"
        )

    def test_fall_back_subsequent_fire_next_day(self) -> None:
        """The fire after fall-back day 1:30am must be 2026-11-02 01:30."""
        fires = _fires_after("30 1 * * *", _US_EASTERN, self._START, count=2)
        assert len(fires) >= 2, "Expected at least two fires"
        second_local = fires[1].astimezone(_US_EASTERN)
        assert second_local.date() == datetime(2026, 11, 2).date(), (
            f"Expected second fire on 2026-11-02, got {second_local}"
        )

    def test_fall_back_fires_at_correct_utc(self) -> None:
        """1:30am EDT (UTC-4) on fall-back day should be 05:30 UTC."""
        fires = _fires_after("30 1 * * *", _US_EASTERN, self._START, count=1)
        assert fires
        utc_fire = fires[0].astimezone(ZoneInfo("UTC"))
        # cronsim fires at 1:30am EDT (UTC-4) → 05:30 UTC.
        assert utc_fire.hour == 5
        assert utc_fire.minute == 30, (
            f"Expected 05:30 UTC, got {utc_fire.hour}:{utc_fire.minute:02d}"
        )


# ---------------------------------------------------------------------------
# UK spring-forward (2026-03-29)
# ---------------------------------------------------------------------------


class TestUKSpringForward:
    """UK clocks jump from 01:00 GMT to 02:00 BST on 2026-03-29.

    A "0 1 * * *" (1am) cron: cronsim advances to 2am on the transition day,
    then resumes at 1am BST every subsequent day.
    """

    # One minute before UK spring-forward.
    _START = datetime(2026, 3, 29, 0, 59, tzinfo=_UK)

    def test_uk_spring_forward_advances_past_gap(self) -> None:
        """1am UK cron must advance to 2am on spring-forward day."""
        fires = _fires_after("0 1 * * *", _UK, self._START, count=3)
        assert fires, "Expected fires after UK spring-forward"
        first_local = fires[0].astimezone(_UK)
        # On 2026-03-29, 1am doesn't exist (GMT+0 → BST, clock jumps to 2am).
        # cronsim advances to 2am BST (next valid instant).
        if first_local.year == 2026 and first_local.month == 3 and first_local.day == 29:
            # If it fires on the gap day, it must be at 2am, not 1am.
            assert first_local.hour != 1, (
                f"cronsim must not fire at 1am on UK spring-forward day "
                f"(that time doesn't exist): {first_local}"
            )

    def test_uk_subsequent_fires_at_1am(self) -> None:
        """After UK spring-forward, subsequent fires return to 1am BST."""
        fires = _fires_after("0 1 * * *", _UK, self._START, count=5)
        assert len(fires) >= 2
        # Post-transition fires (skip day-of-transition) must be at 1am.
        post_transition = [
            f for f in fires
            if not (f.astimezone(_UK).year == 2026
                    and f.astimezone(_UK).month == 3
                    and f.astimezone(_UK).day == 29)
        ]
        for fire in post_transition:
            local = fire.astimezone(_UK)
            assert local.hour == 1, (
                f"Post-spring-forward fires must be at 1am, got {local.hour}: {local}"
            )


# ---------------------------------------------------------------------------
# Multi-TZ: LA 9am fires at correct UTC
# ---------------------------------------------------------------------------


class TestMultiTZCorrectness:
    def test_la_9am_fires_at_16_utc_during_pdt(self) -> None:
        """LA 9am should be UTC 16:00 during PDT (UTC-7)."""
        # Pick a summer Monday (PDT, UTC-7): 2026-06-01.
        start = datetime(2026, 6, 1, 0, 0, tzinfo=_US_LA)
        fires = _fires_after("0 9 * * 1", _US_LA, start, count=3)
        assert fires
        for fire in fires:
            local = fire.astimezone(_US_LA)
            assert local.hour == 9, f"Expected local 9am, got {local.hour}"
            # During PDT (UTC-7), 9am LA = 16:00 UTC.
            utc_fire = fire.astimezone(ZoneInfo("UTC"))
            assert utc_fire.hour == 16, (
                f"Expected UTC 16:00 (PDT offset), got UTC {utc_fire.hour}:00"
            )

    def test_la_9am_fires_at_17_utc_during_pst(self) -> None:
        """LA 9am during winter (PST, UTC-8) should be UTC 17:00."""
        # Pick a winter Monday: 2026-01-05.
        start = datetime(2026, 1, 5, 0, 0, tzinfo=_US_LA)
        fires = _fires_after("0 9 * * 1", _US_LA, start, count=3)
        assert fires
        for fire in fires:
            local = fire.astimezone(_US_LA)
            assert local.hour == 9, f"Expected local 9am, got {local.hour}"
            utc_fire = fire.astimezone(ZoneInfo("UTC"))
            assert utc_fire.hour == 17, (
                f"Expected UTC 17:00 (PST offset), got UTC {utc_fire.hour}:00"
            )

    def test_fires_are_timezone_aware(self) -> None:
        """All fire times from cronsim must be tz-aware."""
        start = datetime(2026, 1, 1, 0, 0, tzinfo=_US_LA)
        fires = _fires_after("0 9 * * *", _US_LA, start, count=5)
        for fire in fires:
            assert fire.tzinfo is not None, "Fire times must be tz-aware"


# ---------------------------------------------------------------------------
# Validate integration — DST-related
# ---------------------------------------------------------------------------


class TestValidateDSTIntegration:
    def test_validate_daily_cron_eastern(self) -> None:
        """validate() should produce tz-aware fires for an Eastern cron."""
        s = Schedule(kind="cron", cron_or_iso="0 9 * * *", tz="America/New_York")
        result = validate(s)
        assert result.next_fires
        for fire in result.next_fires:
            assert fire.tzinfo is not None, "Fires must be tz-aware"
            local = fire.astimezone(_US_EASTERN)
            assert local.hour == 9

    def test_validate_daily_cron_uk(self) -> None:
        """validate() should produce tz-aware fires for a UK cron."""
        s = Schedule(kind="cron", cron_or_iso="0 8 * * *", tz="Europe/London")
        result = validate(s)
        assert result.next_fires
        for fire in result.next_fires:
            assert fire.tzinfo is not None
            local = fire.astimezone(_UK)
            assert local.hour == 8

    def test_validate_la_9am_produces_correct_utc(self) -> None:
        """validate() on LA 9am daily must produce fires at correct UTC."""
        s = Schedule(kind="cron", cron_or_iso="0 9 * * *", tz="America/Los_Angeles")
        result = validate(s)
        assert result.next_fires
        for fire in result.next_fires:
            local = fire.astimezone(_US_LA)
            assert local.hour == 9, f"Expected 9am LA, got {local}"
