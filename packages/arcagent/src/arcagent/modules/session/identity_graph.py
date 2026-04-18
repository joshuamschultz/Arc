"""Identity graph — cross-platform user identity resolution.

Maps (platform, platform_user_id) → stable user_did so the same human
is recognised across Telegram, Slack, Signal, etc. and shares a single
session per SDD §3.3 (D-06).

Architecture:
  - Single SQLite table ``user_identity_links`` colocated with the
    FTS5 session index (same db_path directory).
  - Insert-on-first-seen: calling resolve_user_identity for an unknown
    (platform, user_id) pair deterministically derives a new user_did and
    inserts a row.  The PRIMARY KEY on (platform, platform_user_id) is the
    sole concurrency guard — no application-level locks needed.
  - Every state-changing operation emits a federal audit event via the
    injected telemetry (gateway.identity.link / gateway.identity.unlink).
  - Raw platform_user_id values are NEVER written to audit events; only
    the first 16 hex chars of SHA-256(platform_user_id) appear (LLM02 /
    PII protection).

DID generation strategy:
  arcagent.core.identity.AgentIdentity.generate() is designed for Ed25519
  keypair-anchored agent DIDs.  Human users do NOT own keypairs in this
  model — their DID is a stable identifier derived deterministically from
  the first-seen (platform, platform_user_id) pair so that any future
  cross-platform linking produces a canonical anchor.

  Format: did:arc:user:human/{sha256_prefix_16}
    where sha256_prefix_16 = SHA-256("{platform}:{platform_user_id}")[:16]

  This is intentionally deterministic so that re-creation of the row (e.g.
  after a DB restore) yields the same DID, preventing silent identity
  forks.  The tradeoff is that the DID can be predicted by anyone who knows
  the platform + user_id — which is acceptable because the DID is NOT a
  secret credential; it is an identifier.  Secrets (session tokens, keys)
  are always separate.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid hard runtime dependency; callers inject the real instance.
    from arcagent.core.telemetry import AgentTelemetry

_logger = logging.getLogger("arcagent.modules.session.identity_graph")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS user_identity_links (
    user_did            TEXT NOT NULL,
    platform            TEXT NOT NULL,
    platform_user_id    TEXT NOT NULL,
    linked_at           REAL NOT NULL,
    linked_by_did       TEXT NOT NULL,
    PRIMARY KEY (platform, platform_user_id)
);
CREATE INDEX IF NOT EXISTS ix_user_identity_links_user_did
    ON user_identity_links(user_did);
"""

# Sentinel used as linked_by_did when a row is auto-created on first-seen.
_SYSTEM_DID = "did:arc:system:gateway/identity_graph"


# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """A single (platform, platform_user_id) → user_did mapping row."""

    user_did: str
    platform: str
    platform_user_id: str
    linked_at: float
    linked_by_did: str


# ---------------------------------------------------------------------------
# IdentityGraph
# ---------------------------------------------------------------------------


class IdentityGraph:
    """Cross-platform identity resolution backed by SQLite.

    Thread-safety: each public method opens its own connection for the
    duration of the call and closes it immediately after — the same
    pattern used by SessionIndex.  SQLite's built-in journal (WAL) allows
    concurrent readers and serialises writers without application locks.

    Args:
        db_path: Path to the SQLite database file.  Parent directories are
            created automatically.  The file may be shared with
            SessionIndex (different table; no schema conflict).
        telemetry: Optional AgentTelemetry instance.  When None, audit
            events are silently skipped.  This keeps unit tests that do
            not care about telemetry simple.
    """

    def __init__(
        self,
        db_path: Path,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._telemetry = telemetry
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_user_identity(self, platform: str, platform_user_id: str) -> str:
        """Resolve (platform, platform_user_id) to a stable user_did.

        If the pair is already known the stored user_did is returned.
        If it is not known a new user_did is derived deterministically
        and the row is inserted (insert-on-first-seen).

        The PRIMARY KEY constraint on (platform, platform_user_id) is
        the concurrency guard: if two callers race to insert the same
        pair, SQLite's ``INSERT OR IGNORE`` ensures exactly one row is
        written and both callers observe the same user_did.
        """
        existing = self.lookup_user_did(platform, platform_user_id)
        if existing is not None:
            return existing

        new_did = _derive_user_did(platform, platform_user_id)
        self._insert_or_ignore(
            user_did=new_did,
            platform=platform,
            platform_user_id=platform_user_id,
            linked_by_did=_SYSTEM_DID,
        )
        # Re-read after insert so that if a concurrent writer won the race
        # we return its DID, not ours.
        resolved = self.lookup_user_did(platform, platform_user_id)
        if resolved is None:
            # INSERT OR IGNORE guarantees the row exists after this call.
            # If we somehow can't read it back, the DB is in a bad state.
            msg = (
                f"Failed to resolve identity for {platform}:{platform_user_id} "
                "after insert — database may be corrupt."
            )
            raise RuntimeError(msg)
        self._emit_link_event(resolved, platform, platform_user_id, _SYSTEM_DID)
        return resolved

    def link_identities(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
        linked_by_did: str,
    ) -> None:
        """Explicitly link a (platform, platform_user_id) to an existing user_did.

        Idempotent: if the pair is already linked (to any user_did) the
        call is a no-op.  This avoids both exceptions and silent data
        mutation — the caller must unlink first before re-linking.

        Args:
            user_did: The canonical DID to attach this identity to.
            platform: Platform name (e.g. "slack", "telegram").
            platform_user_id: The platform-native user identifier.
            linked_by_did: DID of the operator or system performing the link.
                Used for federal audit traceability.
        """
        self._insert_or_ignore(
            user_did=user_did,
            platform=platform,
            platform_user_id=platform_user_id,
            linked_by_did=linked_by_did,
        )
        self._emit_link_event(user_did, platform, platform_user_id, linked_by_did)

    def unlink_identity(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
    ) -> None:
        """Remove a (platform, platform_user_id) link from a user_did.

        The user_did itself is never deleted — the link row is removed
        so subsequent resolve calls will treat the pair as new.

        Idempotent: calling on a non-existent row is a no-op.
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM user_identity_links "
                "WHERE user_did = ? AND platform = ? AND platform_user_id = ?",
                (user_did, platform, platform_user_id),
            )
        self._emit_unlink_event(user_did, platform, platform_user_id)

    def lookup_user_did(self, platform: str, platform_user_id: str) -> str | None:
        """Read-only lookup of user_did for a given (platform, platform_user_id).

        Returns None if the pair is not in the graph.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_did FROM user_identity_links "
                "WHERE platform = ? AND platform_user_id = ?",
                (platform, platform_user_id),
            ).fetchone()
        return row[0] if row else None

    def list_links(self, user_did: str) -> list[Link]:
        """Return all linked identities for a given user_did.

        Returns an empty list if the user_did has no rows.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_did, platform, platform_user_id, linked_at, linked_by_did "
                "FROM user_identity_links WHERE user_did = ? ORDER BY linked_at",
                (user_did,),
            ).fetchall()
        return [
            Link(
                user_did=row[0],
                platform=row[1],
                platform_user_id=row[2],
                linked_at=row[3],
                linked_by_did=row[4],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the table and index if they do not exist."""
        with self._connect() as conn:
            conn.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived connection with WAL mode + timeout.

        A new connection is opened on each call (not cached) so that
        concurrent threads and processes each get their own SQLite
        handle, preventing the 'ProgrammingError: cannot operate on a
        closed database' issue that arises with shared connections.
        """
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _insert_or_ignore(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
        linked_by_did: str,
    ) -> None:
        """Insert a link row; silently ignore if PRIMARY KEY already exists."""
        linked_at = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_identity_links "
                "(user_did, platform, platform_user_id, linked_at, linked_by_did) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_did, platform, platform_user_id, linked_at, linked_by_did),
            )

    def _emit_link_event(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
        linked_by_did: str,
    ) -> None:
        """Emit gateway.identity.link audit event if telemetry is wired."""
        if self._telemetry is None:
            return
        self._telemetry.audit_event(
            "gateway.identity.link",
            {
                "user_did": user_did,
                "platform": platform,
                # SHA-256 prefix — never log raw PII (LLM02 mitigation)
                "platform_user_id_hash": _hash_user_id(platform_user_id),
                "linked_by_did": linked_by_did,
                "ts": time.time(),
            },
        )

    def _emit_unlink_event(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
    ) -> None:
        """Emit gateway.identity.unlink audit event if telemetry is wired."""
        if self._telemetry is None:
            return
        self._telemetry.audit_event(
            "gateway.identity.unlink",
            {
                "user_did": user_did,
                "platform": platform,
                "platform_user_id_hash": _hash_user_id(platform_user_id),
                "ts": time.time(),
            },
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _derive_user_did(platform: str, platform_user_id: str) -> str:
    """Deterministically derive a user DID from (platform, platform_user_id).

    Format: did:arc:user:human/{sha256_prefix_16}
      where sha256_prefix_16 = SHA-256("{platform}:{platform_user_id}")[:16]

    The 16-char (64-bit) prefix gives 1-in-2^64 collision probability,
    acceptable for a non-credential identifier.  The full input is kept
    secret — only the hash is stored in the DID.

    TODO: If arcagent.core.identity exposes a dedicated user-DID factory
    in the future, replace this with that factory.  The format must remain
    did:arc:user:human/{id} per Arc DID spec.
    """
    raw = f"{platform}:{platform_user_id}"
    prefix = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"did:arc:user:human/{prefix}"


def _hash_user_id(platform_user_id: str, chars: int = 16) -> str:
    """Return the first ``chars`` hex chars of SHA-256(platform_user_id).

    Used in audit events so raw PII never enters log storage.
    16 hex chars = 64 bits of entropy — sufficient to identify the row
    in an investigation without disclosing the original value.
    """
    return hashlib.sha256(platform_user_id.encode()).hexdigest()[:chars]
