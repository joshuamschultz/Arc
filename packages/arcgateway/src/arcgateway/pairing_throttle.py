"""PairingThrottle — rate-limit and platform-lockout logic for DM pairing.

Extracted from PairingStore to give the throttle policy a dedicated home.
PairingStore composes this class rather than owning the logic directly.

Responsibilities:
    - check_rate_limit(conn, platform, user_hash, now)  → raises PairingRateLimited
    - check_platform_full(conn, platform, now)          → raises PairingPlatformFull
    - check_platform_locked(conn, platform, now)        → raises PairingPlatformLocked
    - record_failure(conn, platform, now)               → inserts failure + maybe lockout
    - is_locked(conn, platform, now)                    → bool

All methods are synchronous and operate on an already-open sqlite3.Connection.
Callers (PairingStore) are responsible for acquiring the asyncio.Lock before
entering any of these methods.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from arcgateway.pairing import (
    _LOCKOUT_DURATION_SECONDS,
    _LOCKOUT_FAILURE_THRESHOLD,
    _MAX_PENDING_PER_PLATFORM,
    _TTL_SECONDS,
    _USER_RATE_LIMIT_SECONDS,
    PairingPlatformFull,
    PairingPlatformLocked,
    PairingRateLimited,
)

_logger = logging.getLogger("arcgateway.pairing_throttle")


class PairingThrottle:
    """Encapsulates rate-limit and platform-lockout policy for pairing.

    All methods operate on an already-open sqlite3.Connection.  The caller
    is responsible for holding the asyncio.Lock before entering these methods
    to prevent concurrent mutation.

    Attributes:
        _max_pending:     Maximum pending codes per platform.
        _rate_limit_secs: Per-user minting rate limit window (seconds).
        _lockout_secs:    Platform lockout duration (seconds).
        _lockout_thresh:  Failed attempts before lockout triggers.
        _ttl_secs:        Code TTL used for failure-window calculation.
    """

    def __init__(
        self,
        *,
        max_pending: int = _MAX_PENDING_PER_PLATFORM,
        rate_limit_seconds: float = _USER_RATE_LIMIT_SECONDS,
        lockout_duration_seconds: float = _LOCKOUT_DURATION_SECONDS,
        lockout_threshold: int = _LOCKOUT_FAILURE_THRESHOLD,
        ttl_seconds: float = _TTL_SECONDS,
    ) -> None:
        """Initialise PairingThrottle with configurable policy values.

        Args:
            max_pending:              Max pending codes per platform.
            rate_limit_seconds:       User re-mint rate limit window.
            lockout_duration_seconds: Duration of platform lockout.
            lockout_threshold:        Failures before lockout triggers.
            ttl_seconds:              Code TTL (used for failure-window calc).
        """
        self._max_pending = max_pending
        self._rate_limit_secs = rate_limit_seconds
        self._lockout_secs = lockout_duration_seconds
        self._lockout_thresh = lockout_threshold
        self._ttl_secs = ttl_seconds

    # -----------------------------------------------------------------------
    # Public checks (raise on violation)
    # -----------------------------------------------------------------------

    def check_platform_locked(
        self,
        conn: sqlite3.Connection,
        platform: str,
        now: float,
    ) -> None:
        """Raise PairingPlatformLocked if the platform has an active lockout.

        Args:
            conn:     Open DB connection.
            platform: Platform name.
            now:      Current unix timestamp.

        Raises:
            PairingPlatformLocked: If the platform is currently locked.
        """
        if self.is_locked(conn, platform, now):
            raise PairingPlatformLocked(
                f"Platform {platform!r} is locked due to failed approval attempts"
            )

    def check_rate_limit(
        self,
        conn: sqlite3.Connection,
        platform: str,
        user_hash: str,
        now: float,
    ) -> None:
        """Raise PairingRateLimited if the user minted a code recently.

        Args:
            conn:      Open DB connection.
            platform:  Platform name.
            user_hash: Hashed platform user ID.
            now:       Current unix timestamp.

        Raises:
            PairingRateLimited: If the user minted within the rate-limit window.
        """
        recent_count: int = conn.execute(
            """SELECT COUNT(*) FROM pairing_codes
               WHERE platform = ? AND user_hash = ?
                 AND minted_at > ? AND consumed = 0""",
            (platform, user_hash, now - self._rate_limit_secs),
        ).fetchone()[0]

        if recent_count > 0:
            raise PairingRateLimited(
                f"User already has a pending code on {platform!r}; "
                f"wait {int(self._rate_limit_secs) // 60} minutes"
            )

    def check_platform_full(
        self,
        conn: sqlite3.Connection,
        platform: str,
        now: float,
    ) -> None:
        """Raise PairingPlatformFull if the platform cap is reached.

        Args:
            conn:     Open DB connection.
            platform: Platform name.
            now:      Current unix timestamp.

        Raises:
            PairingPlatformFull: If the platform already has max pending codes.
        """
        pending_count: int = conn.execute(
            """SELECT COUNT(*) FROM pairing_codes
               WHERE platform = ? AND expires_at > ? AND consumed = 0""",
            (platform, now),
        ).fetchone()[0]

        if pending_count >= self._max_pending:
            raise PairingPlatformFull(
                f"Platform {platform!r} already has {self._max_pending} pending pairing codes"
            )

    # -----------------------------------------------------------------------
    # Failure recording
    # -----------------------------------------------------------------------

    def record_failure(
        self,
        conn: sqlite3.Connection,
        platform: str,
        now: float,
        audit_fn: Any | None = None,
    ) -> None:
        """Record a failed approval attempt and trigger lockout if threshold reached.

        Inserts a failure row and, when recent failures reach the threshold,
        inserts/replaces a lockout record.

        Args:
            conn:     Open DB connection.
            platform: Platform where the failure occurred.
            now:      Current unix timestamp.
            audit_fn: Optional callable(event_type, details) for audit emission.
        """
        conn.execute(
            "INSERT INTO pairing_failures(platform, attempted_at) VALUES (?, ?)",
            (platform, now),
        )

        recent_failures: int = conn.execute(
            """SELECT COUNT(*) FROM pairing_failures
               WHERE platform = ? AND attempted_at > ?""",
            (platform, now - self._ttl_secs),
        ).fetchone()[0]

        if recent_failures >= self._lockout_thresh:
            locked_until = now + self._lockout_secs
            conn.execute(
                """INSERT OR REPLACE INTO pairing_lockouts(platform, locked_until)
                   VALUES (?, ?)""",
                (platform, locked_until),
            )
            _logger.warning(
                "Platform %r locked for 1h after %d failed approval attempts",
                platform,
                recent_failures,
            )
            if audit_fn is not None:
                audit_fn(
                    "gateway.pairing.locked_out",
                    {"platform": platform, "locked_until": locked_until},
                )

    # -----------------------------------------------------------------------
    # Lockout query
    # -----------------------------------------------------------------------

    def is_locked(
        self,
        conn: sqlite3.Connection,
        platform: str,
        now: float,
    ) -> bool:
        """Return True if the platform has an active lockout record.

        Args:
            conn:     Open DB connection.
            platform: Platform to check.
            now:      Current unix timestamp.

        Returns:
            True if locked, False otherwise.
        """
        row = conn.execute(
            "SELECT locked_until FROM pairing_lockouts WHERE platform = ?",
            (platform,),
        ).fetchone()
        if row is None:
            return False
        return float(row[0]) > now
