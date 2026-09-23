"""Replay protection is a public leaf primitive shared by agent and team."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arctrust import ReplayCache


def test_public_replay_cache_rejects_replay_and_stale_timestamp() -> None:
    cache = ReplayCache(window_seconds=60)
    now = datetime.now(UTC).isoformat()
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    assert cache.check_and_record("first", now)
    assert not cache.check_and_record("first", now)
    assert not cache.check_and_record("old", stale)
