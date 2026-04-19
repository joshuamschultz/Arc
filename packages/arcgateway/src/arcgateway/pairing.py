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

Federal / enterprise / personal signature semantics (T1.8.3, M3 gap-close):

    Federal:    signature is REQUIRED.  Missing or invalid → approval refused,
                PairingSignatureInvalid raised; failure recorded against the
                platform lockout counter.  The operator's Ed25519 pubkey is
                resolved via ``arctrust.trust_store.load_operator_pubkey``.
    Enterprise: signature is OPTIONAL but recommended.  When supplied it is
                verified; when absent a WARN audit event is emitted
                (``gateway.pairing.signature_missing``) but approval proceeds.
    Personal:   signature is ignored.

Challenge format
----------------
Operators sign the challenge

    sha256(code.encode() + minted_at_iso.encode())

where ``minted_at_iso`` is the ISO-8601 UTC timestamp of the minted_at float
rendered by ``_iso_minted_at()``.  The signing side must compute the same
ISO string locally from the pairing-record's minted_at seconds (which the
gateway displays in ``arc gateway pair list``).

Audit events emitted (SDD §4.2, federal-tier signature additions):
    - gateway.pairing.minted:                {platform, user_hash, code_id,
      expires_at, minted_at_iso}
    - gateway.pairing.approved:              {code_id, approver_did, signed_by_did, platform}
    - gateway.pairing.denied:                {code_id, attempted_at, reason, platform}
    - gateway.pairing.expired:               {code_id, platform}
    - gateway.pairing.locked_out:            {platform, locked_until}
    - gateway.pairing.signature_verified:    {code_id, signed_by_did, platform}
    - gateway.pairing.signature_invalid:     {code_id, approver_did, platform, reason}
    - gateway.pairing.signature_missing:     {code_id, approver_did, platform, tier}

Federal multi-instance Postgres backend (T1.8.4):
    - Deferred. See PostgresPairingStore stub below.

Performance note (Wave-2 perf review):
    All sync-sqlite-in-async operations are now wrapped with asyncio.to_thread()
    so the event loop is never blocked by file I/O.  This closes the Wave-2
    high-severity finding (sync DB ops in async context).

Security properties:
    - Raw user IDs are never persisted; only SHA-256 first-16-char hashes.
    - Codes themselves are not logged; code_id = sha256(code)[:16] in audit.
    - DB file created with 0600 permissions (owner read/write only).
    - Failed attempts (including signature failures) are recorded per-platform
      to detect brute-force.

Composition:
    - PairingThrottle  (arcgateway.pairing_throttle) — rate-limit + lockout policy.
    - PairingSignatureVerifier (arcgateway.pairing_signature) — Ed25519 verification.
    Rate-limit and signature logic are tested independently via those classes.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

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

# Max retries for unique-code generation.  At 32^8 ~= 1.1T search space the
# birthday-paradox collision probability per attempt is roughly 2^-40
# (assuming < ~1k pending codes), so 10 attempts corresponds to a practical
# collision probability under ~10^-11 — effectively never.  Keeping a small
# bound guards against pathological RNG failure without risking an infinite
# loop in the mint path.
_MAX_CODE_GEN_ATTEMPTS = 10

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


class PairingSignatureInvalid(PairingError):
    """Raised when an approver signature fails verification.

    At federal tier this blocks approval.  The failure is recorded against
    the platform's lockout counter so repeated bogus signatures cannot be
    used to probe for valid codes.
    """


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
        signed_by_did:         DID that produced the verified Ed25519 signature
                               on approval (None until approval lands, or at
                               non-federal tier when signature was omitted).
    """

    code: str
    platform: str
    platform_user_id_hash: str  # SHA-256 first 16 chars — no raw PII stored
    minted_at: float
    expires_at: float
    signed_by_did: str | None = None


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
    consumed        INTEGER NOT NULL DEFAULT 0,
    signed_by_did   TEXT
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


_ADD_SIGNED_BY_DID_COLUMN = "ALTER TABLE pairing_codes ADD COLUMN signed_by_did TEXT"


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


def _iso_minted_at(minted_at: float) -> str:
    """Return the ISO-8601 UTC representation (seconds precision) of minted_at.

    The signing CLI and the verifier must agree on this string exactly.
    We use ``timespec="seconds"`` so sub-second drift between mint time and
    sign time does not cause signature failures.
    """
    return (
        _dt.datetime.fromtimestamp(minted_at, tz=_dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_pairing_challenge(code: str, minted_at: float) -> bytes:
    """Return the Ed25519 challenge bytes an operator must sign.

    ``challenge = sha256(code.encode() + minted_at_iso.encode())``

    Exposed as public API so the operator-side ``arc gateway pair sign``
    command can produce identical bytes without duplicating the formula.
    """
    minted_at_iso = _iso_minted_at(minted_at)
    h = hashlib.sha256()
    h.update(code.encode("utf-8"))
    h.update(minted_at_iso.encode("utf-8"))
    return h.digest()


# ---------------------------------------------------------------------------
# PairingStore
# ---------------------------------------------------------------------------


Tier = Literal["personal", "enterprise", "federal"]


class PairingStore:
    """SQLite-backed store for DM pairing codes.

    Thread safety: Uses asyncio.Lock to serialise DB writes.
    asyncio cooperativeness: All sqlite3 calls are executed via
    ``asyncio.to_thread()`` to avoid blocking the event loop (closes
    Wave-2 perf review high-severity finding).

    Composition:
        - PairingThrottle  (arcgateway.pairing_throttle) — rate-limit + lockout.
        - PairingSignatureVerifier (arcgateway.pairing_signature) — Ed25519 verify.

    Attributes:
        _db_path:      Path to the SQLite database file.
        _federal_tier: If True, verify_and_consume requires a valid Ed25519 signature.
        _tier:         Full tier string (``"personal" | "enterprise" | "federal"``).
        _lock:         asyncio.Lock serialising writes (prevents double-consume race).
        _trust_dir:    Optional override for the operator trust store.
        _throttle:     PairingThrottle for rate-limit + lockout policy.
        _sig:          PairingSignatureVerifier for Ed25519 verification.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        federal_tier: bool = False,
        tier: Tier | None = None,
        trust_dir: Path | None = None,
    ) -> None:
        """Initialise PairingStore.

        Args:
            db_path:       Path to the SQLite DB. Defaults to
                           ~/.arc/gateway/pairing.db.
            federal_tier:  Legacy flag — if True and ``tier`` is not set, tier
                           defaults to ``"federal"``.  Preserved for backwards
                           compatibility with existing callers.
            tier:          Explicit tier.  Takes precedence over ``federal_tier``.
                           ``"federal"`` requires a valid Ed25519 signature on
                           approval; ``"enterprise"`` warns when absent but still
                           accepts the approval; ``"personal"`` ignores signatures.
            trust_dir:     Override the directory scanned by the trust store.
                           Defaults to ``~/.arc/trust``.
        """
        if db_path is None:
            db_path = Path.home() / ".arc" / "gateway" / "pairing.db"

        if tier is not None:
            resolved_tier: Tier = tier
        elif federal_tier:
            resolved_tier = "federal"
        else:
            resolved_tier = "personal"

        self._db_path = db_path
        self._tier: Tier = resolved_tier
        self._federal_tier = resolved_tier == "federal"
        self._trust_dir = trust_dir
        self._lock = asyncio.Lock()
        self._telemetry: Any = None

        # Composed policy helpers — extracted for independent testability.
        from arcgateway.pairing_signature import PairingSignatureVerifier
        from arcgateway.pairing_throttle import PairingThrottle

        self._throttle = PairingThrottle()
        self._sig = PairingSignatureVerifier(tier=resolved_tier, trust_dir=trust_dir)

        self._init_db()

    def attach_telemetry(self, telemetry: Any) -> None:
        """Attach an AgentTelemetry-compatible sink for audit forwarding.

        After attachment every ``_audit`` call also invokes
        ``telemetry.audit_event``.  Kept separate from ``__init__`` to
        avoid a circular import between arcgateway and arcagent.core.
        """
        self._telemetry = telemetry

    def _init_db(self) -> None:
        """Create DB file, apply schema, set 0600 permissions, migrate column.

        Idempotent: all DDL uses IF NOT EXISTS.  The ``signed_by_did`` column
        is added via best-effort ALTER TABLE so existing deployments upgrade
        without data loss.
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.executescript(_SCHEMA_SQL)
            try:
                conn.execute(_ADD_SIGNED_BY_DID_COLUMN)
            except sqlite3.OperationalError:
                pass
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

        DB operations run via asyncio.to_thread() to avoid blocking the event
        loop (closes Wave-2 perf review high-severity finding).

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
            result = await asyncio.to_thread(
                self._mint_code_sync, platform, user_hash
            )

        code_str, minted_at, expires_at = result
        self._audit(
            "gateway.pairing.minted",
            {
                "platform": platform,
                "user_hash": user_hash,
                "code_id": _code_id(code_str),
                "expires_at": expires_at,
                "minted_at_iso": _iso_minted_at(minted_at),
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
        signature: bytes | None = None,
        *,
        platform_hint: str | None = None,
    ) -> PairingCode | None:
        """Attempt to approve and consume a pairing code.

        Federal tier:
          - ``approver_did`` REQUIRED.
          - ``signature`` REQUIRED — Ed25519 over
            ``sha256(code + minted_at_iso)``.
          - Missing/invalid signature → ``PairingSignatureInvalid`` raised,
            failure recorded against the platform lockout counter.

        Enterprise tier:
          - ``approver_did`` optional (WARN audit if absent).
          - ``signature`` optional.  When present, verified.
          - Missing signature → warn audit + proceed.

        Personal tier:
          - Signature ignored; approval succeeds on code validity alone.

        Args:
            code:           8-char pairing code to approve.
            approver_did:   DID of the approving operator.
            signature:      Ed25519 signature bytes.
            platform_hint:  Platform name for failure attribution when the
                            code is not found in the DB.

        Returns:
            PairingCode if valid; None on invalid/expired/consumed code.

        Raises:
            PairingSignatureInvalid: Federal tier with missing or bad
                signature; enterprise tier with present-but-bad signature.
        """
        if self._federal_tier and approver_did is None:
            fail_platform = platform_hint if platform_hint else "unknown"
            async with self._lock:
                await asyncio.to_thread(
                    self._record_failure_threaded, fail_platform
                )
            self._audit(
                "gateway.pairing.signature_invalid",
                {
                    "code_id": _code_id(code),
                    "approver_did": None,
                    "platform": fail_platform,
                    "reason": "missing_approver_did",
                },
            )
            raise PairingSignatureInvalid(
                "federal tier requires approver_did + signature"
            )

        async with self._lock:
            result = await asyncio.to_thread(
                self._verify_and_consume_sync,
                code, approver_did, signature, platform_hint,
            )

        if result is None:
            return None

        pairing_code, code_str, approver_did_out, sig_was_present = result
        self._audit(
            "gateway.pairing.approved",
            {
                "code_id": _code_id(code_str),
                "approver_did": approver_did_out,
                "signed_by_did": approver_did_out if sig_was_present else None,
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
        return await asyncio.to_thread(self._is_platform_locked_sync, platform, now)

    async def list_pending(self) -> list[PairingCode]:
        """Return all unexpired, unconsumed pairing codes across all platforms.

        Returns:
            List of PairingCode objects, newest first.
        """
        return await asyncio.to_thread(self._list_pending_sync)

    async def revoke(self, code: str) -> bool:
        """Revoke a pairing code (operator action, e.g. `arc gateway pair revoke`).

        Args:
            code: 8-char pairing code to revoke.

        Returns:
            True if the code existed and was revoked; False if not found.
        """
        async with self._lock:
            revoked = await asyncio.to_thread(self._revoke_sync, code)

        if revoked:
            self._audit("gateway.pairing.revoked", {"code_id": _code_id(code)})
        return revoked

    async def cleanup_expired(self) -> int:
        """Delete all expired (regardless of consumed status) pairing codes.

        Called by a background sweep task to prevent unbounded DB growth.

        Returns:
            Number of rows deleted.
        """
        async with self._lock:
            removed = await asyncio.to_thread(self._cleanup_expired_sync)

        if removed > 0:
            _logger.debug("cleanup_expired: removed %d expired pairing codes", removed)
        return removed

    # -----------------------------------------------------------------------
    # Thread-safe synchronous worker methods (run via asyncio.to_thread)
    # -----------------------------------------------------------------------

    def _mint_code_sync(
        self,
        platform: str,
        user_hash: str,
    ) -> tuple[str, float, float]:
        """Synchronous mint worker — runs in a thread via asyncio.to_thread.

        Returns:
            Tuple (code_str, minted_at, expires_at).

        Raises:
            PairingPlatformLocked, PairingRateLimited, PairingPlatformFull.
        """
        now = time.time()
        with self._connect() as conn:
            self._throttle.check_platform_locked(conn, platform, now)
            self._throttle.check_rate_limit(conn, platform, user_hash, now)
            self._throttle.check_platform_full(conn, platform, now)

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

        return code_str, minted_at, expires_at

    def _verify_and_consume_sync(
        self,
        code: str,
        approver_did: str | None,
        signature: bytes | None,
        platform_hint: str | None,
    ) -> tuple[PairingCode, str, str | None, bool] | None:
        """Synchronous verify+consume worker — runs in a thread.

        Returns:
            (PairingCode, code_str, approver_did, sig_was_present) on success;
            None on bad/expired/consumed code.

        Raises:
            PairingSignatureInvalid on signature policy violation.
        """
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT code, platform, user_hash, minted_at, expires_at,
                          consumed, signed_by_did
                   FROM pairing_codes WHERE code = ?""",
                (code,),
            ).fetchone()

            if row is None or row["consumed"] != 0 or row["expires_at"] <= now:
                self._record_bad_code_conn(conn, row, code, platform_hint, now)
                conn.commit()
                return None

            # Delegate signature policy to PairingSignatureVerifier.
            self._sig.enforce_policy(
                conn=conn,
                row=row,
                code=code,
                approver_did=approver_did,
                signature=signature,
                now=now,
                record_failure_fn=self._throttle.record_failure,
                audit_fn=self._audit,
            )

            conn.execute(
                "UPDATE pairing_codes SET consumed = 1, signed_by_did = ? WHERE code = ?",
                (approver_did, code),
            )
            conn.commit()

        pairing_code = PairingCode(
            code=row["code"],
            platform=row["platform"],
            platform_user_id_hash=row["user_hash"],
            minted_at=row["minted_at"],
            expires_at=row["expires_at"],
            signed_by_did=approver_did,
        )
        return pairing_code, code, approver_did, signature is not None

    def _record_failure_threaded(self, platform: str) -> None:
        """Record a failure in a thread-safe, standalone DB operation.

        Args:
            platform: Platform name for failure attribution.
        """
        now = time.time()
        with self._connect() as conn:
            self._throttle.record_failure(conn, platform, now, self._audit)
            conn.commit()

    def _is_platform_locked_sync(self, platform: str, now: float) -> bool:
        """Check platform lockout in a thread.

        Args:
            platform: Platform name.
            now:      Current unix timestamp.

        Returns:
            True if locked.
        """
        with self._connect() as conn:
            return self._throttle.is_locked(conn, platform, now)

    def _list_pending_sync(self) -> list[PairingCode]:
        """Fetch pending codes from DB in a thread.

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

    def _revoke_sync(self, code: str) -> bool:
        """Mark a code as consumed in a thread.

        Args:
            code: Pairing code to revoke.

        Returns:
            True if revoked; False if not found.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE pairing_codes SET consumed = 1 WHERE code = ? AND consumed = 0",
                (code,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def _cleanup_expired_sync(self) -> int:
        """Delete expired codes and stale failure records in a thread.

        Returns:
            Number of pairing_codes rows deleted.
        """
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM pairing_codes WHERE expires_at <= ?",
                (now,),
            )
            conn.execute(
                "DELETE FROM pairing_failures WHERE attempted_at < ?",
                (now - 2 * _TTL_SECONDS,),
            )
            conn.commit()
            return cursor.rowcount

    # -----------------------------------------------------------------------
    # Private helpers (synchronous — called from within thread workers)
    # -----------------------------------------------------------------------

    def _record_bad_code_conn(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row | None,
        code: str,
        platform_hint: str | None,
        now: float,
    ) -> None:
        """Record failure + emit the right denied/expired audit event."""
        if row is not None:
            fail_platform = row["platform"]
        elif platform_hint is not None:
            fail_platform = platform_hint
        else:
            fail_platform = "unknown"

        self._throttle.record_failure(conn, fail_platform, now, self._audit)

        if row is None:
            reason = "unknown"
            event = "gateway.pairing.denied"
        elif row["consumed"] != 0:
            reason = "already_consumed"
            event = "gateway.pairing.denied"
        else:
            reason = "expired"
            event = "gateway.pairing.expired"

        details: dict[str, Any] = {
            "code_id": _code_id(code),
            "attempted_at": now,
            "platform": fail_platform,
        }
        if event == "gateway.pairing.denied":
            details["reason"] = reason
        self._audit(event, details)

    def _mint_unique_code(self, conn: sqlite3.Connection) -> str:
        """Generate an 8-char code that does not already exist in the DB.

        Args:
            conn: Open DB connection for uniqueness check.

        Returns:
            A unique 8-char code string.
        """
        for _ in range(_MAX_CODE_GEN_ATTEMPTS):
            candidate = _mint_code_chars()
            exists = conn.execute(
                "SELECT 1 FROM pairing_codes WHERE code = ?",
                (candidate,),
            ).fetchone()
            if exists is None:
                return candidate
        raise PairingError(
            f"Failed to generate a unique pairing code after "
            f"{_MAX_CODE_GEN_ATTEMPTS} attempts"
        )

    def _audit(self, event_type: str, details: dict[str, Any]) -> None:
        """Emit a structured audit log entry.

        Routes through the shared ``arcgateway.telemetry.emit_audit`` helper
        so pairing events share the same schema (``audit_event`` +
        ``audit_data`` extras) as stream_bridge and adapter audit events.

        When a telemetry instance has been attached via ``attach_telemetry``
        the event is also forwarded to ``telemetry.audit_event`` so that
        OTel sinks (NIST 800-53 AU-9 tamper-evidence) receive pairing
        events alongside the rest of the gateway's audit trail.

        Security: Never includes raw codes — only code_id (sha256 first 16).
        """
        from arcgateway.telemetry import emit_audit

        audit_logger = logging.getLogger("arcgateway.pairing.audit")
        emit_audit(audit_logger, event_type, details)

        telemetry = self._telemetry
        if telemetry is not None:
            try:
                telemetry.audit_event(event_type, details)
            except Exception:
                # Telemetry failure must not break the pairing path; the
                # stdlib-logged audit above is the tamper-resistant fallback.
                _logger.warning(
                    "PairingStore._audit: telemetry forward failed for %s",
                    event_type,
                    exc_info=True,
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
