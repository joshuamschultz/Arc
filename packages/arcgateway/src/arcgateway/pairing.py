"""DM pairing — one-time code minting and approval for unknown users.

Lifted from Hermes ``gateway/pairing.py`` and adapted for Arc's federal-first
security model. When an unknown user messages the gateway, the adapter calls
``PairingStore.mint_code()`` to generate an 8-char one-time code, then DMs it
to the user. The operator approves via ``arc gateway pair approve <code>``.

Design (SDD §3.1 DM Pairing):
    - 8-char codes from PAIRING_ALPHABET (no ambiguous 0/O/1/I)
    - 1h TTL; max 3 pending codes per platform
    - 1 code request per user per 10 min (prevents spam)
    - 5 failed approval attempts → 1h platform lockout
    - Storage: SQLite at ~/.arc/gateway/pairing.db (0600 perms)
    - The raw pairing code is a SECRET — never logged; log code_id (sha256 first 16)

Audit events emitted (SDD §4.2):
    - gateway.pairing.minted:    {platform, user_hash, code_id, expires_at}
    - gateway.pairing.approved:  {code_id, approver_did}
    - gateway.pairing.denied:    {code_id, attempted_at}
    - gateway.pairing.expired:   {code_id}
    - gateway.pairing.locked_out: {platform, locked_until}

Federal tier additions (T1.8.3):
    - verify_and_consume() requires non-None approver_did at federal tier.
    - Signature verification is a stub that passes if approver_did is provided.
    - TODO(M2): Implement real Ed25519 signature verification against
      arcagent.core.identity DID keypairs. See PLAN T1.8.3 and SDD §3.1.

Federal multi-instance Postgres backend (T1.8.4):
    - Deferred. See PostgresPairingStore stub below.
    - TODO(T1.8.4): Replace SQLite backend with PostgresPairingStore using
      SELECT FOR UPDATE (pessimistic lock) on approval operations.

Security properties:
    - Raw user IDs are never persisted; only SHA-256 first-16-char hashes.
    - Codes themselves are not logged; code_id = sha256(code)[:16] in audit.
    - DB file created with 0600 permissions (owner read/write only).
    - Failed attempts are recorded per-platform to detect brute-force.
    - Failure attribution: when a bad code is attempted, the gateway always
      knows which platform the attempt arrived from (the DM channel). Callers
      pass the platform_hint so failures are attributed correctly even when
      the code itself is unknown. Without a hint, falls back to "unknown"
      (harmless — the lockout key is per-platform).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_logger = logging.getLogger("arcgateway.pairing")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 32-char unambiguous alphabet — no 0/O/1/I (Hermes pattern)
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_CODE_LENGTH = 8
_TTL_SECONDS = 3600  # 1 hour
_MAX_PENDING_PER_PLATFORM = 3
_USER_RATE_LIMIT_SECONDS = 600  # 10 minutes
_LOCKOUT_FAILURE_THRESHOLD = 5
_LOCKOUT_DURATION_SECONDS = 3600  # 1 hour

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PairingError(Exception):
    """Base class for pairing errors."""


class PairingRateLimited(PairingError):
    """Raised when a user requests a second code within 10 minutes."""


class PairingPlatformFull(PairingError):
    """Raised when a platform already has 3 pending codes."""


class PairingPlatformLocked(PairingError):
    """Raised when a platform is locked due to too many failed attempts."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class PairingCode(BaseModel):
    """Immutable record of a minted pairing code.

    Attributes:
        code:                  8-char code from PAIRING_ALPHABET (SECRET — never log).
        platform:              Platform name (e.g. "telegram", "slack").
        platform_user_id_hash: SHA-256 first 16 chars of raw platform user ID.
        minted_at:             Unix timestamp when code was minted.
        expires_at:            Unix timestamp when code expires (minted_at + TTL).
    """

    code: str
    platform: str
    platform_user_id_hash: str  # SHA-256 first 16 chars — no raw PII stored
    minted_at: float
    expires_at: float


# ---------------------------------------------------------------------------
# Schema SQL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS pairing_codes (
    code            TEXT PRIMARY KEY,
    platform        TEXT NOT NULL,
    user_hash       TEXT NOT NULL,
    minted_at       REAL NOT NULL,
    expires_at      REAL NOT NULL,
    consumed        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pairing_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    attempted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pairing_lockouts (
    platform    TEXT PRIMARY KEY,
    locked_until REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pairing_codes_platform_expires
    ON pairing_codes(platform, expires_at, consumed);

CREATE INDEX IF NOT EXISTS idx_pairing_failures_platform_time
    ON pairing_failures(platform, attempted_at);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_user_id(platform: str, platform_user_id: str) -> str:
    """Return SHA-256 first 16 hex chars of "{platform}:{user_id}".

    Including platform ensures telegram:123 and slack:123 are distinct hashes.
    Never stores raw user IDs in any persistent medium (GDPR / NIST AU-3).
    """
    raw = f"{platform}:{platform_user_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _code_id(code: str) -> str:
    """Return SHA-256 first 16 hex chars of the code for audit logging.

    The code itself is a SECRET and must never appear in logs. code_id is
    a safe, deterministic opaque identifier for audit trails.
    """
    return hashlib.sha256(code.encode()).hexdigest()[:16]


def _mint_code_chars() -> str:
    """Generate an 8-char one-time code using cryptographically secure randomness."""
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(_CODE_LENGTH))


def _set_db_permissions(db_path: Path) -> None:
    """Chmod the SQLite DB file to 0600 (owner read/write only).

    Called after initial creation and after any temp-file atomic replace.
    """
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        _logger.warning("Could not set 0600 permissions on %s", db_path)


# ---------------------------------------------------------------------------
# PairingStore
# ---------------------------------------------------------------------------


class PairingStore:
    """SQLite-backed store for DM pairing codes.

    Thread safety: Uses asyncio.Lock to serialise DB writes.
    asyncio cooperativeness: All sqlite3 calls are synchronous but fast
    (local file, no network). No event-loop blocking issue in practice.
    For high-throughput federal multi-instance deployments, use PostgresPairingStore.

    Attributes:
        _db_path:     Path to the SQLite database file.
        _federal_tier: If True, verify_and_consume requires approver_did.
        _lock:        asyncio.Lock serialising writes (prevents double-consume race).
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        federal_tier: bool = False,
    ) -> None:
        """Initialise PairingStore.

        Args:
            db_path:       Path to the SQLite DB. Defaults to
                           ~/.arc/gateway/pairing.db.
            federal_tier:  If True, verify_and_consume() requires approver_did.
        """
        if db_path is None:
            db_path = Path.home() / ".arc" / "gateway" / "pairing.db"

        self._db_path = db_path
        self._federal_tier = federal_tier
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create DB file, apply schema, and set 0600 permissions.

        Called once at construction. Idempotent (all DDL is IF NOT EXISTS).
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()
        _set_db_permissions(self._db_path)

    def _connect(self) -> sqlite3.Connection:
        """Open a new SQLite connection with WAL mode.

        Returns a fresh connection; callers are responsible for closing it.
        Using per-operation connections avoids shared-state issues under
        asyncio's cooperative scheduling.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def mint_code(
        self,
        platform: str,
        platform_user_id: str,
    ) -> PairingCode:
        """Mint a new 8-char one-time pairing code.

        Checks (in order before minting):
        1. Platform lockout — raises PairingPlatformLocked if active.
        2. Per-user rate limit — raises PairingRateLimited if same user minted
           within the last 10 min.
        3. Platform pending cap — raises PairingPlatformFull if >= 3 pending.

        Args:
            platform:           Platform identifier (e.g. "telegram").
            platform_user_id:   Raw user ID on the platform (hashed before storage).

        Returns:
            PairingCode with the minted code.

        Raises:
            PairingPlatformLocked:  Platform is in a 1h lockout.
            PairingRateLimited:     Same user already minted within 10 min.
            PairingPlatformFull:    Platform already has 3 pending codes.
        """
        user_hash = _hash_user_id(platform, platform_user_id)

        async with self._lock:
            now = time.time()

            with self._connect() as conn:
                # 1. Check platform lockout
                if self._is_locked_conn(conn, platform, now):
                    raise PairingPlatformLocked(
                        f"Platform {platform!r} is locked due to failed approval attempts"
                    )

                # 2. Per-user rate limit: user must not have a code minted in last 10min
                recent_count = conn.execute(
                    """SELECT COUNT(*) FROM pairing_codes
                       WHERE platform = ? AND user_hash = ?
                         AND minted_at > ? AND consumed = 0""",
                    (platform, user_hash, now - _USER_RATE_LIMIT_SECONDS),
                ).fetchone()[0]
                if recent_count > 0:
                    raise PairingRateLimited(
                        f"User already has a pending code on {platform!r}; "
                        f"wait {_USER_RATE_LIMIT_SECONDS // 60} minutes"
                    )

                # 3. Platform pending cap
                pending_count = conn.execute(
                    """SELECT COUNT(*) FROM pairing_codes
                       WHERE platform = ? AND expires_at > ? AND consumed = 0""",
                    (platform, now),
                ).fetchone()[0]
                if pending_count >= _MAX_PENDING_PER_PLATFORM:
                    raise PairingPlatformFull(
                        f"Platform {platform!r} already has {_MAX_PENDING_PER_PLATFORM} "
                        "pending pairing codes"
                    )

                # Mint the code (retry on collision — astronomically rare at 32^8)
                code_str = self._mint_unique_code(conn)

                minted_at = now
                expires_at = now + _TTL_SECONDS

                conn.execute(
                    """INSERT INTO pairing_codes
                       (code, platform, user_hash, minted_at, expires_at, consumed)
                       VALUES (?, ?, ?, ?, ?, 0)""",
                    (code_str, platform, user_hash, minted_at, expires_at),
                )
                conn.commit()

        # Emit audit event (outside lock — I/O is acceptable here)
        self._audit(
            "gateway.pairing.minted",
            {
                "platform": platform,
                "user_hash": user_hash,
                "code_id": _code_id(code_str),  # NEVER log the code itself
                "expires_at": expires_at,
            },
        )

        return PairingCode(
            code=code_str,
            platform=platform,
            platform_user_id_hash=user_hash,
            minted_at=minted_at,
            expires_at=expires_at,
        )

    async def verify_and_consume(
        self,
        code: str,
        approver_did: str | None = None,
        *,
        platform_hint: str | None = None,
    ) -> PairingCode | None:
        """Attempt to approve and consume a pairing code.

        On success: marks the code as consumed, returns PairingCode.
        On failure: records a failed attempt (may trigger lockout), returns None.

        Federal tier: approver_did is required. If None, returns None immediately.
        Signature verification: stub that passes if approver_did is non-None.
        TODO(M2 / T1.8.3): Replace stub with real Ed25519 signature verification
        using arcagent.core.identity. The signed challenge is
        sha256(code.encode() + str(minted_at).encode()). See PLAN T1.8.3.

        Failure attribution: failures are counted per-platform to detect
        brute-force attempts. When the code is valid, its stored platform is
        used. When the code is unknown/invalid, ``platform_hint`` is used (the
        gateway always knows which platform the attempt arrived from — it's the
        DM channel where the user sent the code). Falls back to "unknown" when
        no hint is provided, which is safe but reduces per-platform precision.

        Args:
            code:           8-char pairing code to approve.
            approver_did:   DID of the approving operator. Required at federal tier.
            platform_hint:  Platform name for failure attribution when the code
                            is not found in the DB (e.g. "telegram").

        Returns:
            PairingCode if valid and not expired; None otherwise.
        """
        # Federal tier: reject immediately if no approver DID supplied
        if self._federal_tier and approver_did is None:
            _logger.warning(
                "Federal tier: verify_and_consume called without approver_did "
                "for code_id=%s — rejected",
                _code_id(code),
            )
            return None

        async with self._lock:
            now = time.time()

            with self._connect() as conn:
                row = conn.execute(
                    """SELECT code, platform, user_hash, minted_at, expires_at, consumed
                       FROM pairing_codes WHERE code = ?""",
                    (code,),
                ).fetchone()

                # Unknown code or already consumed or expired
                if row is None or row["consumed"] != 0 or row["expires_at"] <= now:
                    # Attribute failure to the known platform (from code row) or the
                    # provided hint (caller's platform context) or "unknown" as fallback.
                    fail_platform: str
                    if row is not None:
                        fail_platform = row["platform"]
                    elif platform_hint is not None:
                        fail_platform = platform_hint
                    else:
                        fail_platform = "unknown"

                    self._record_failure_conn(conn, fail_platform, now)
                    conn.commit()

                    if row is None:
                        self._audit(
                            "gateway.pairing.denied",
                            {
                                "code_id": _code_id(code),
                                "attempted_at": now,
                                "reason": "unknown",
                                "platform": fail_platform,
                            },
                        )
                    elif row["consumed"] != 0:
                        self._audit(
                            "gateway.pairing.denied",
                            {
                                "code_id": _code_id(code),
                                "attempted_at": now,
                                "reason": "already_consumed",
                                "platform": fail_platform,
                            },
                        )
                    else:
                        self._audit(
                            "gateway.pairing.expired",
                            {"code_id": _code_id(code), "platform": fail_platform},
                        )
                    return None

                # TODO(M2 / T1.8.3): Verify Ed25519 signature from approver_did.
                # The signed challenge is: sha256(code.encode() + str(minted_at).encode()).
                # Until M2 identity module lands, we accept any non-None approver_did.
                # See arcagent.core.identity for keypair primitives.
                # Reference: PLAN.md T1.8.3, SDD §3.1 DM Pairing federal additions.
                if self._federal_tier and approver_did is not None:
                    # Stub: always passes. Real verification in M2.
                    pass

                # Mark consumed atomically
                conn.execute(
                    "UPDATE pairing_codes SET consumed = 1 WHERE code = ?",
                    (code,),
                )
                conn.commit()

            pairing_code = PairingCode(
                code=row["code"],
                platform=row["platform"],
                platform_user_id_hash=row["user_hash"],
                minted_at=row["minted_at"],
                expires_at=row["expires_at"],
            )

        self._audit(
            "gateway.pairing.approved",
            {
                "code_id": _code_id(code),
                "approver_did": approver_did,
                "platform": pairing_code.platform,
            },
        )

        return pairing_code

    async def is_platform_locked(self, platform: str) -> bool:
        """Return True if the platform is currently in a lockout window.

        Args:
            platform: Platform name to check.

        Returns:
            True if locked, False otherwise.
        """
        now = time.time()
        with self._connect() as conn:
            return self._is_locked_conn(conn, platform, now)

    async def list_pending(self) -> list[PairingCode]:
        """Return all unexpired, unconsumed pairing codes across all platforms.

        Returns:
            List of PairingCode objects, newest first.
        """
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT code, platform, user_hash, minted_at, expires_at
                   FROM pairing_codes
                   WHERE expires_at > ? AND consumed = 0
                   ORDER BY minted_at DESC""",
                (now,),
            ).fetchall()
        return [
            PairingCode(
                code=r["code"],
                platform=r["platform"],
                platform_user_id_hash=r["user_hash"],
                minted_at=r["minted_at"],
                expires_at=r["expires_at"],
            )
            for r in rows
        ]

    async def revoke(self, code: str) -> bool:
        """Revoke a pairing code (operator action, e.g. `arc gateway pair revoke`).

        Marks the code as consumed so it cannot be approved. This is logically
        identical to consuming it — the difference is semantic/audit only.

        Args:
            code: 8-char pairing code to revoke.

        Returns:
            True if the code existed and was revoked; False if not found.
        """
        async with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE pairing_codes SET consumed = 1 WHERE code = ? AND consumed = 0",
                    (code,),
                )
                conn.commit()
                revoked = cursor.rowcount > 0

        if revoked:
            self._audit("gateway.pairing.revoked", {"code_id": _code_id(code)})
        return revoked

    async def cleanup_expired(self) -> int:
        """Delete all expired (regardless of consumed status) pairing codes.

        Called by a background sweep task to prevent unbounded DB growth.

        Returns:
            Number of rows deleted.
        """
        now = time.time()
        async with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM pairing_codes WHERE expires_at <= ?",
                    (now,),
                )
                # Also prune old failure records (> 2h old)
                conn.execute(
                    "DELETE FROM pairing_failures WHERE attempted_at < ?",
                    (now - 2 * _TTL_SECONDS,),
                )
                conn.commit()
                removed = cursor.rowcount

        if removed > 0:
            _logger.debug("cleanup_expired: removed %d expired pairing codes", removed)

        return removed

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _is_locked_conn(
        self,
        conn: sqlite3.Connection,
        platform: str,
        now: float,
    ) -> bool:
        """Check platform lockout within an existing connection.

        Args:
            conn:     Open DB connection (caller holds the asyncio lock).
            platform: Platform to check.
            now:      Current unix timestamp.

        Returns:
            True if the platform has an active lockout record.
        """
        row = conn.execute(
            "SELECT locked_until FROM pairing_lockouts WHERE platform = ?",
            (platform,),
        ).fetchone()
        if row is None:
            return False
        return float(row[0]) > now

    def _record_failure_conn(
        self,
        conn: sqlite3.Connection,
        platform: str,
        now: float,
    ) -> None:
        """Record a failed approval attempt and trigger lockout if threshold reached.

        Threshold: 5 failures within the last hour → insert/replace 1h lockout.

        Args:
            conn:     Open DB connection (caller holds the asyncio lock).
            platform: Platform where the failure occurred.
            now:      Current unix timestamp.
        """
        conn.execute(
            "INSERT INTO pairing_failures(platform, attempted_at) VALUES (?, ?)",
            (platform, now),
        )

        # Count failures within the last 1h window
        recent_failures = conn.execute(
            """SELECT COUNT(*) FROM pairing_failures
               WHERE platform = ? AND attempted_at > ?""",
            (platform, now - _TTL_SECONDS),
        ).fetchone()[0]

        if recent_failures >= _LOCKOUT_FAILURE_THRESHOLD:
            locked_until = now + _LOCKOUT_DURATION_SECONDS
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
            self._audit(
                "gateway.pairing.locked_out",
                {"platform": platform, "locked_until": locked_until},
            )

    def _mint_unique_code(self, conn: sqlite3.Connection) -> str:
        """Generate an 8-char code that does not already exist in the DB.

        Collision probability at 32^8 ≈ 1.1 trillion is negligible; retry loop
        is purely defensive. Expected iterations: 1.

        Args:
            conn: Open DB connection for uniqueness check.

        Returns:
            A unique 8-char code string.
        """
        for _ in range(10):  # Safety bound; practically always 1 iteration
            candidate = _mint_code_chars()
            exists = conn.execute(
                "SELECT 1 FROM pairing_codes WHERE code = ?",
                (candidate,),
            ).fetchone()
            if exists is None:
                return candidate
        # Astronomically unlikely — more likely a DB/config issue
        raise PairingError("Failed to generate a unique pairing code after 10 attempts")

    def _audit(self, event_type: str, details: dict[str, Any]) -> None:
        """Emit a structured audit log entry.

        Uses Python's logging subsystem directly because PairingStore has no
        reference to AgentTelemetry (arcagent.core.telemetry). The audit logger
        at arcgateway.pairing.audit is a structured sink that operators can
        route to their SIEM of choice.

        Security: Never includes raw codes — only code_id (sha256 first 16).
        """
        audit_logger = logging.getLogger("arcgateway.pairing.audit")
        audit_logger.info(
            "event_type=%s details=%r",
            event_type,
            details,
        )


# ---------------------------------------------------------------------------
# PostgresPairingStore stub (T1.8.4 — deferred)
# ---------------------------------------------------------------------------


class PostgresPairingStore:
    """Postgres-backed PairingStore for federal multi-instance deployments.

    Uses SELECT FOR UPDATE (pessimistic lock) on approval so that two gateway
    instances cannot double-consume the same code under concurrent load.

    TODO(T1.8.4): Implement. Required for federal deployments with >1 gateway
    instance behind a load balancer. See PLAN.md T1.8.4 and SDD §3.1 DM Pairing
    (Federal multi-instance Postgres backend). Requires asyncpg or psycopg3.
    Connection string from Vault (federal tier) per D-14.
    """

    def __init__(self, dsn: str, *, federal_tier: bool = True) -> None:
        raise NotImplementedError(
            "PostgresPairingStore is not yet implemented. "
            "Use PairingStore (SQLite) for single-instance deployments. "
            "See PLAN.md T1.8.4 for the multi-instance Postgres backend design."
        )
