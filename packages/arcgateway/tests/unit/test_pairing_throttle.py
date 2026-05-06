"""Unit tests for PairingThrottle — rate-limit and platform-lockout logic."""

from __future__ import annotations

import itertools
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from arcgateway.pairing import (
    _ADD_SIGNED_BY_DID_COLUMN,
    _SCHEMA_SQL,
    PairingPlatformFull,
    PairingPlatformLocked,
    PairingRateLimited,
)
from arcgateway.pairing_throttle import PairingThrottle


def _make_db() -> tuple[sqlite3.Connection, Path]:
    """Create an in-memory-like temp SQLite DB for testing."""
    tmp = tempfile.mktemp(suffix=".db")  # noqa: S306 — test-only fixture, not security-sensitive
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    try:
        conn.execute(_ADD_SIGNED_BY_DID_COLUMN)
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn, Path(tmp)


# Monotonic counter so successive seeds within one test produce unique
# `code` values; using id(user_hash) collided when the same hash was
# reused across multiple seeds in a single test (pre-existing flake).
_CODE_COUNTER = itertools.count(1)


def _seed_pending_code(
    conn: sqlite3.Connection,
    platform: str,
    user_hash: str,
    minted_at: float,
    expires_at: float,
) -> None:
    code = f"CODE{next(_CODE_COUNTER):08d}"
    conn.execute(
        """INSERT INTO pairing_codes(code, platform, user_hash, minted_at, expires_at, consumed)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (code, platform, user_hash, minted_at, expires_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# check_platform_locked
# ---------------------------------------------------------------------------


class TestCheckPlatformLocked:
    def test_no_lockout_passes(self) -> None:
        conn, _ = _make_db()
        throttle = PairingThrottle()
        # No lock record — should not raise
        throttle.check_platform_locked(conn, "telegram", time.time())

    def test_active_lockout_raises(self) -> None:
        conn, _ = _make_db()
        now = time.time()
        locked_until = now + 3600
        conn.execute(
            "INSERT OR REPLACE INTO pairing_lockouts(platform, locked_until) VALUES (?, ?)",
            ("telegram", locked_until),
        )
        conn.commit()

        throttle = PairingThrottle()
        with pytest.raises(PairingPlatformLocked):
            throttle.check_platform_locked(conn, "telegram", now)

    def test_expired_lockout_passes(self) -> None:
        conn, _ = _make_db()
        now = time.time()
        # Expired lock: locked_until is in the past
        conn.execute(
            "INSERT OR REPLACE INTO pairing_lockouts(platform, locked_until) VALUES (?, ?)",
            ("telegram", now - 1),
        )
        conn.commit()
        throttle = PairingThrottle()
        # Should NOT raise
        throttle.check_platform_locked(conn, "telegram", now)


# ---------------------------------------------------------------------------
# check_rate_limit
# ---------------------------------------------------------------------------


class TestCheckRateLimit:
    def test_no_recent_code_passes(self) -> None:
        conn, _ = _make_db()
        throttle = PairingThrottle(rate_limit_seconds=600)
        # No prior codes — should not raise
        throttle.check_rate_limit(conn, "telegram", "userhash1", time.time())

    def test_recent_code_raises(self) -> None:
        conn, _ = _make_db()
        now = time.time()
        _seed_pending_code(conn, "telegram", "userhash2", now - 60, now + 3540)

        throttle = PairingThrottle(rate_limit_seconds=600)
        with pytest.raises(PairingRateLimited):
            throttle.check_rate_limit(conn, "telegram", "userhash2", now)

    def test_old_code_beyond_window_passes(self) -> None:
        conn, _ = _make_db()
        now = time.time()
        # Code minted 20 minutes ago, window is 10 minutes
        _seed_pending_code(conn, "telegram", "userhash3", now - 1200, now + 2400)

        throttle = PairingThrottle(rate_limit_seconds=600)
        # Should NOT raise — code is outside the rate-limit window
        throttle.check_rate_limit(conn, "telegram", "userhash3", now)


# ---------------------------------------------------------------------------
# check_platform_full
# ---------------------------------------------------------------------------


class TestCheckPlatformFull:
    def test_below_cap_passes(self) -> None:
        conn, _ = _make_db()
        throttle = PairingThrottle(max_pending=3)
        # 0 pending codes — should not raise
        throttle.check_platform_full(conn, "telegram", time.time())

    def test_at_cap_raises(self) -> None:
        conn, _ = _make_db()
        now = time.time()
        for i in range(3):
            _seed_pending_code(conn, "telegram", f"user{i}", now - i, now + 3600)

        throttle = PairingThrottle(max_pending=3)
        with pytest.raises(PairingPlatformFull):
            throttle.check_platform_full(conn, "telegram", now)

    def test_expired_codes_not_counted(self) -> None:
        """Expired codes do not count toward the platform cap."""
        conn, _ = _make_db()
        now = time.time()
        # Insert 3 expired codes
        for i in range(3):
            _seed_pending_code(conn, "telegram", f"user{i}", now - 7200, now - 1)

        throttle = PairingThrottle(max_pending=3)
        # Expired codes should NOT count — should not raise
        throttle.check_platform_full(conn, "telegram", now)


# ---------------------------------------------------------------------------
# record_failure + lockout trigger
# ---------------------------------------------------------------------------


class TestRecordFailure:
    def test_single_failure_no_lockout(self) -> None:
        conn, _ = _make_db()
        throttle = PairingThrottle(lockout_threshold=5)
        now = time.time()
        throttle.record_failure(conn, "telegram", now)
        conn.commit()

        assert not throttle.is_locked(conn, "telegram", now)

    def test_five_failures_trigger_lockout(self) -> None:
        conn, _ = _make_db()
        throttle = PairingThrottle(lockout_threshold=5)
        now = time.time()
        for _ in range(5):
            throttle.record_failure(conn, "telegram", now)
        conn.commit()

        assert throttle.is_locked(conn, "telegram", now)

    def test_failures_on_different_platforms_isolated(self) -> None:
        """Failures on platform A do not trigger lockout on platform B."""
        conn, _ = _make_db()
        throttle = PairingThrottle(lockout_threshold=5)
        now = time.time()
        for _ in range(5):
            throttle.record_failure(conn, "telegram", now)
        conn.commit()

        assert not throttle.is_locked(conn, "slack", now)

    def test_record_failure_calls_audit_fn_on_lockout(self) -> None:
        """When lockout triggers, the audit_fn callback is invoked."""
        conn, _ = _make_db()
        audit_calls: list[tuple[str, dict]] = []

        def mock_audit(event: str, details: dict) -> None:
            audit_calls.append((event, details))

        throttle = PairingThrottle(lockout_threshold=5)
        now = time.time()
        for _ in range(5):
            throttle.record_failure(conn, "telegram", now, audit_fn=mock_audit)
        conn.commit()

        lockout_events = [e for e, _ in audit_calls if e == "gateway.pairing.locked_out"]
        assert len(lockout_events) >= 1
