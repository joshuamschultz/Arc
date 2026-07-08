"""DST regression suite for the live cron-firing path.

The scheduler engine (``SchedulerEngine._should_fire_cron``) and the model
validator both evaluate cron expressions with croniter in **UTC** — there is
no per-expression timezone field. That makes firing immune to the wall-clock
DST bug croniter historically exhibited when iterating inside a DST zone:
because evaluation never happens in a DST zone, a daily cron fires at the same
UTC instant every day and neither skips nor double-fires across a transition.

These tests pin that behavior on the real engine path so a future engine change
(e.g. introducing zone-aware firing) cannot silently regress it.

Reference: US/Eastern spring-forward 2026-03-08 (02:00 -> 03:00) and
fall-back 2026-11-01 (02:00 -> 01:00) — both irrelevant to UTC firing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from arcagent.modules.scheduler.models import ScheduleEntry, ScheduleMetadata
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from tests.unit.modules.scheduler.conftest import make_config


def _engine() -> SchedulerEngine:
    return SchedulerEngine(
        store=MagicMock(),
        config=make_config(),
        telemetry=MagicMock(),
        agent_run_fn=AsyncMock(),
    )


def _cron_entry(expression: str, last_run: datetime) -> ScheduleEntry:
    return ScheduleEntry(
        id="sched_dst",
        type="cron",
        prompt="Daily digest",
        expression=expression,
        metadata=ScheduleMetadata(last_run=last_run.isoformat()),
    )


class TestCronFiringUTCAcrossSpringForward:
    """A "0 6 * * *" daily cron fires at 06:00 UTC every day, unaffected by the
    US spring-forward on 2026-03-08."""

    _EXPR = "0 6 * * *"

    def test_does_not_fire_before_next_utc_instant(self) -> None:
        engine = _engine()
        entry = _cron_entry(self._EXPR, datetime(2026, 3, 8, 6, 0, tzinfo=UTC))
        now = datetime(2026, 3, 9, 5, 59, tzinfo=UTC)
        assert engine._should_fire_cron(entry, now) is False

    def test_fires_at_exact_next_utc_instant(self) -> None:
        engine = _engine()
        entry = _cron_entry(self._EXPR, datetime(2026, 3, 8, 6, 0, tzinfo=UTC))
        now = datetime(2026, 3, 9, 6, 0, tzinfo=UTC)
        assert engine._should_fire_cron(entry, now) is True

    def test_no_double_fire_within_same_utc_day(self) -> None:
        """Having just fired at 06:00 UTC on the transition day, the cron must
        not fire again until the next 06:00 UTC — no DST-induced double-fire."""
        engine = _engine()
        entry = _cron_entry(self._EXPR, datetime(2026, 3, 8, 6, 0, tzinfo=UTC))
        # Any time later that same UTC day (past the spring-forward instant).
        now = datetime(2026, 3, 8, 23, 30, tzinfo=UTC)
        assert engine._should_fire_cron(entry, now) is False


class TestCronFiringUTCAcrossFallBack:
    """A "30 5 * * *" daily cron fires once at 05:30 UTC across the US fall-back
    on 2026-11-01 — croniter's ambiguous-hour bug never applies in UTC."""

    _EXPR = "30 5 * * *"

    def test_fires_exactly_once_across_fall_back(self) -> None:
        engine = _engine()
        entry = _cron_entry(self._EXPR, datetime(2026, 10, 31, 5, 30, tzinfo=UTC))
        # Just after the next scheduled UTC instant on fall-back day.
        assert engine._should_fire_cron(entry, datetime(2026, 11, 1, 5, 30, tzinfo=UTC)) is True
        # Fire recorded; must not fire again later the same UTC day.
        fired = _cron_entry(self._EXPR, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        assert engine._should_fire_cron(fired, datetime(2026, 11, 1, 23, 0, tzinfo=UTC)) is False
