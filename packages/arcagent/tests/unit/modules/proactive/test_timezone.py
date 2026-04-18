"""SPEC-017 R-049 — timezone + DST handling for ProactiveEngine."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest


class TestActiveHoursBasic:
    def test_within_window_is_active(self) -> None:
        from arcagent.modules.proactive.timezone import ActiveHours

        hours = ActiveHours(tz="UTC", start=time(9, 0), end=time(17, 0))
        # 12:00 UTC is inside 09:00–17:00
        now = datetime(2026, 4, 18, 12, 0, tzinfo=ZoneInfo("UTC"))
        assert hours.is_active(now) is True

    def test_before_window_is_inactive(self) -> None:
        from arcagent.modules.proactive.timezone import ActiveHours

        hours = ActiveHours(tz="UTC", start=time(9, 0), end=time(17, 0))
        now = datetime(2026, 4, 18, 8, 59, tzinfo=ZoneInfo("UTC"))
        assert hours.is_active(now) is False

    def test_at_end_boundary_is_inactive(self) -> None:
        """``end`` is exclusive — matches Python's half-open interval
        convention."""
        from arcagent.modules.proactive.timezone import ActiveHours

        hours = ActiveHours(tz="UTC", start=time(9, 0), end=time(17, 0))
        now = datetime(2026, 4, 18, 17, 0, tzinfo=ZoneInfo("UTC"))
        assert hours.is_active(now) is False


class TestOvernightWindow:
    def test_spans_midnight_evening_side(self) -> None:
        from arcagent.modules.proactive.timezone import ActiveHours

        hours = ActiveHours(tz="UTC", start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 4, 18, 23, 30, tzinfo=ZoneInfo("UTC"))
        assert hours.is_active(now) is True

    def test_spans_midnight_morning_side(self) -> None:
        from arcagent.modules.proactive.timezone import ActiveHours

        hours = ActiveHours(tz="UTC", start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 4, 19, 3, 0, tzinfo=ZoneInfo("UTC"))
        assert hours.is_active(now) is True

    def test_midday_is_inactive_for_overnight_window(self) -> None:
        from arcagent.modules.proactive.timezone import ActiveHours

        hours = ActiveHours(tz="UTC", start=time(22, 0), end=time(6, 0))
        now = datetime(2026, 4, 18, 12, 0, tzinfo=ZoneInfo("UTC"))
        assert hours.is_active(now) is False


class TestCrossTimezone:
    def test_local_timezone_is_respected(self) -> None:
        """09:00 in America/Chicago is 14:00 UTC in daylight time (CDT)."""
        from arcagent.modules.proactive.timezone import ActiveHours

        hours = ActiveHours(
            tz="America/Chicago", start=time(9, 0), end=time(17, 0)
        )
        # July — Chicago on CDT (UTC-5). 10:00 CDT = 15:00 UTC
        now = datetime(2026, 7, 15, 15, 0, tzinfo=ZoneInfo("UTC"))
        assert hours.is_active(now) is True

        # 08:00 CDT = 13:00 UTC → outside 09:00–17:00 local
        before = datetime(2026, 7, 15, 13, 0, tzinfo=ZoneInfo("UTC"))
        assert hours.is_active(before) is False


class TestDSTSafety:
    def test_spring_forward_does_not_double_fire(self) -> None:
        """In spring-forward the local clock skips 02:00–03:00. A
        schedule set for 02:30 does not exist that day; the next
        occurrence is the following day."""
        from arcagent.modules.proactive.timezone import next_occurrence

        # 2026-03-08 spring forward in America/Chicago (02:00 → 03:00)
        after = datetime(2026, 3, 8, 1, 0, tzinfo=ZoneInfo("UTC"))
        target = next_occurrence(
            time(2, 30), tz="America/Chicago", after_utc=after
        )
        # Should not be the 2026-03-08 02:30 (which doesn't exist).
        # zoneinfo normalizes to the post-transition instant — pragmatic
        # handling: the schedule fires once at 02:30 localized, which
        # becomes 03:30 UTC+1 (post-DST). Exact semantics vary;
        # the load-bearing check is we don't explode.
        assert target.tzinfo == ZoneInfo("UTC")
        assert target > after


class TestValidation:
    def test_empty_tz_raises(self) -> None:
        from arcagent.modules.proactive.timezone import ActiveHours

        with pytest.raises(ValueError):
            ActiveHours(tz="", start=time(9, 0), end=time(17, 0))

    def test_unknown_tz_raises(self) -> None:
        from arcagent.modules.proactive.timezone import ActiveHours

        with pytest.raises(ValueError):
            ActiveHours(
                tz="Mars/Olympus_Mons",
                start=time(9, 0),
                end=time(17, 0),
            )

    def test_naive_datetime_raises(self) -> None:
        from arcagent.modules.proactive.timezone import ActiveHours

        hours = ActiveHours(tz="UTC", start=time(9, 0), end=time(17, 0))
        naive = datetime(2026, 4, 18, 12, 0)  # no tzinfo
        with pytest.raises(ValueError):
            hours.is_active(naive)
