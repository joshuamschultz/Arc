"""Replay protection is a public leaf primitive shared by agent and team."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arctrust import ReplayCache
from arctrust import replay as replay_module
from arctrust.replay import CLOCK_SKEW_SECONDS


def test_public_replay_cache_rejects_replay_and_stale_timestamp() -> None:
    cache = ReplayCache(window_seconds=60)
    now = datetime.now(UTC).isoformat()
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    assert cache.check_and_record("first", now)
    assert not cache.check_and_record("first", now)
    assert not cache.check_and_record("old", stale)


def test_future_stamped_nonce_kept_until_its_own_timestamp_ages_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonce is remembered while its ts can still pass the freshness check.

    Pruning by arrival time alone would forget a future-stamped nonce one
    window after it arrived, while its ts is still fresh, so the same
    request could be replayed.
    """
    window = 60
    arrival = datetime.now(UTC)
    clock = _pin_clock(monkeypatch, arrival)
    cache = ReplayCache(window_seconds=window)
    ts = (arrival + timedelta(seconds=30)).isoformat()
    assert cache.check_and_record("future", ts)

    clock[0] = arrival + timedelta(seconds=window + 5)
    assert not cache.check_and_record("future", ts)


def test_nonce_forgotten_once_its_timestamp_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory stays bounded: after ts + window the entry is pruned (ts is stale anyway)."""
    window = 60
    arrival = datetime.now(UTC)
    clock = _pin_clock(monkeypatch, arrival)
    cache = ReplayCache(window_seconds=window)
    assert cache.check_and_record("n", (arrival + timedelta(seconds=30)).isoformat())

    clock[0] = arrival + timedelta(seconds=30 + window + 1)
    cache.check_and_record("other", clock[0].isoformat())
    assert len(cache) == 1


def test_timestamp_beyond_clock_skew_ahead_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every caller (MCP door, team messenger, app principals) gets the future
    bound: a far-future ts would otherwise pin its nonce for years and could be
    replayed after a restart while still 'fresh'."""
    now = datetime.now(UTC)
    _pin_clock(monkeypatch, now)
    cache = ReplayCache(window_seconds=300)
    assert cache.check_and_record("near", (now + timedelta(seconds=29)).isoformat())
    assert not cache.check_and_record("far", (now + timedelta(seconds=31)).isoformat())
    assert not cache.check_and_record("years", (now + timedelta(days=3650)).isoformat())
    assert len(cache) == 1


def _pin_clock(monkeypatch: pytest.MonkeyPatch, start: datetime) -> list[datetime]:
    """Pin the wall clock ``ReplayCache`` reads (an external boundary)."""
    current = [start]

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]  # fake clock keeps datetime's signature
            return current[0] if tz is None else current[0].astimezone(tz)

    monkeypatch.setattr(replay_module, "datetime", _Clock)
    return current


def test_future_timestamp_beyond_clock_skew_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ts further ahead than the clock-skew bound is refused, and burns no nonce."""
    arrival = datetime.now(UTC)
    _pin_clock(monkeypatch, arrival)
    cache = ReplayCache()
    too_far = (arrival + timedelta(seconds=CLOCK_SKEW_SECONDS + 1)).isoformat()
    assert not cache.check_and_record("ahead", too_far)
    assert len(cache) == 0


def test_future_timestamp_within_clock_skew_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    arrival = datetime.now(UTC)
    _pin_clock(monkeypatch, arrival)
    cache = ReplayCache()
    at_bound = (arrival + timedelta(seconds=CLOCK_SKEW_SECONDS)).isoformat()
    assert cache.check_and_record("drift", at_bound)


def test_clock_skew_bound_is_the_principal_auth_bound() -> None:
    """One definition: signed requests and the replay cache share the skew constant."""
    from arctrust import principal_auth

    assert CLOCK_SKEW_SECONDS == 30
    assert principal_auth.CLOCK_SKEW_SECONDS is CLOCK_SKEW_SECONDS
