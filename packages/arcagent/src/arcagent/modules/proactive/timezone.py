"""Timezone handling for ProactiveEngine — SPEC-017 R-049.

Schedules are stored in UTC; user-facing config uses IANA timezone
names (``America/Chicago``, ``Europe/Berlin``, etc.). Conversion
happens at schedule creation and during active-hours evaluation.

Overnight windows (``start=22:00, end=06:00``) are detected
automatically — ``end < start`` means "overnight". DST transitions
are handled by :mod:`zoneinfo`: skipped times (spring forward) are
not fired; repeated times (fall back) are not fired twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ActiveHours:
    """Config for schedule active-hours gating.

    Both ``start`` and ``end`` are local times in ``tz``. When
    ``end < start``, the window is overnight (e.g. 22:00 → 06:00
    spans midnight).
    """

    tz: str
    start: time
    end: time

    def __post_init__(self) -> None:
        if not self.tz:
            msg = "tz must be a non-empty IANA name"
            raise ValueError(msg)
        try:
            ZoneInfo(self.tz)
        except ZoneInfoNotFoundError as err:
            msg = f"Unknown timezone: {self.tz!r}"
            raise ValueError(msg) from err

    def is_active(self, now_utc: datetime) -> bool:
        """Return True if ``now_utc`` falls within this window.

        ``now_utc`` must be timezone-aware and anchored to UTC.
        """
        if now_utc.tzinfo is None:
            msg = "now_utc must be timezone-aware"
            raise ValueError(msg)
        local = now_utc.astimezone(ZoneInfo(self.tz))
        current = local.timetz().replace(tzinfo=None)
        if self.start <= self.end:
            # Normal window: start <= current < end
            return self.start <= current < self.end
        # Overnight: current >= start OR current < end
        return current >= self.start or current < self.end


def next_occurrence(
    start_local: time,
    *,
    tz: str,
    after_utc: datetime,
) -> datetime:
    """Return the next UTC timestamp at which ``start_local`` fires.

    Used to convert a schedule's "run at 09:00 America/Chicago"
    declaration into a concrete UTC monotonic anchor. Handles DST:
    :mod:`zoneinfo` automatically picks the post-transition instant
    when the nominal time falls in a gap.
    """
    if after_utc.tzinfo is None:
        msg = "after_utc must be timezone-aware"
        raise ValueError(msg)
    zone = ZoneInfo(tz)
    local_now = after_utc.astimezone(zone)
    target_today = local_now.replace(
        hour=start_local.hour,
        minute=start_local.minute,
        second=start_local.second,
        microsecond=0,
    )
    if target_today <= local_now:
        target_today = target_today + timedelta(days=1)
    return target_today.astimezone(ZoneInfo("UTC"))


__all__ = ["ActiveHours", "next_occurrence"]
