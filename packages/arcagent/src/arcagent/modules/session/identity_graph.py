"""Identity graph — cross-platform user identity resolution.

Maps (platform, platform_user_id) -> stable user_did so the same human
is recognised across Telegram, Slack, Signal, etc. and shares a single
session per SDD section 3.3 (D-06).

Architecture:
  - Single SQLite table ``user_identity_links`` colocated with the
    FTS5 session index (same db_path directory).
  - Insert-on-first-seen: calling resolve_user_identity for an unknown
    (platform, user_id) pair deterministically derives a new user_did and
    inserts a row.  The PRIMARY KEY on (platform, platform_user_id) is the
    sole concurrency guard.
  - Every state-changing operation emits a federal audit event via the
    injected telemetry (gateway.identity.link / gateway.identity.unlink).
  - Raw platform_user_id values are NEVER written to audit events; only
    the first 16 hex chars of SHA-256(platform_user_id) appear (LLM02 /
    PII protection).

Performance (SPEC-018 Wave B1):
  - One long-lived sqlite3.Connection(check_same_thread=False) opened in
    __init__ with PRAGMA journal_mode=WAL applied once.
  - LRU in-memory cache (OrderedDict, max 10000 entries) for read-heavy
    lookup_user_did paths.  Protected by threading.Lock.
  - Write operations (link/unlink) invalidate the affected key.

DID generation strategy:
  Format: did:arc:user:human/{sha256_prefix_16}
    where sha256_prefix_16 = SHA-256("{platform}:{platform_user_id}")[:16]
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry

_logger = logging.getLogger("arcagent.modules.session.identity_graph")

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

_SYSTEM_DID = "did:arc:system:gateway/identity_graph"

# LRU cache capacity
_LRU_MAX_SIZE = 10_000


@dataclass(frozen=True)
class Link:
    """A single (platform, platform_user_id) -> user_did mapping row."""

    user_did: str
    platform: str
    platform_user_id: str
    linked_at: float
    linked_by_did: str


class IdentityGraph:
    """Cross-platform identity resolution backed by SQLite.

    Thread-safety:
      - A single long-lived sqlite3.Connection(check_same_thread=False)
        is opened in __init__.  WAL mode serialises concurrent writers.
      - The LRU cache is guarded by threading.Lock.

    Async variants:
      resolve_user_identity_async, lookup_user_did_async,
      link_identities_async, unlink_identity_async, list_links_async
      offload their sync counterparts to asyncio.to_thread.
    """

    def __init__(
        self,
        db_path: Path,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._telemetry = telemetry
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=30
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._cache_lock: threading.Lock = threading.Lock()
        self._conn_lock: threading.Lock = threading.Lock()

        self._ensure_schema()

    def close(self) -> None:
        """Close the long-lived SQLite connection. Idempotent."""
        try:
            self._conn.close()
        except Exception:
            pass

    def resolve_user_identity(self, platform: str, platform_user_id: str) -> str:
        """Resolve (platform, platform_user_id) to a stable user_did.

        Insert-on-first-seen with PRIMARY KEY concurrency guard.
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
        resolved = self.lookup_user_did(platform, platform_user_id)
        if resolved is None:
            msg = (
                f"Failed to resolve identity for {platform}:{platform_user_id} "
                "after insert -- database may be corrupt."
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

        Idempotent. Invalidates the LRU cache key.
        """
        self._invalidate_cache(platform, platform_user_id)
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
        """Remove a (platform, platform_user_id) link. Idempotent."""
        self._invalidate_cache(platform, platform_user_id)
        with self._conn_lock:
            self._conn.execute(
                "DELETE FROM user_identity_links "
                "WHERE user_did = ? AND platform = ? AND platform_user_id = ?",
                (user_did, platform, platform_user_id),
            )
            self._conn.commit()
        self._emit_unlink_event(user_did, platform, platform_user_id)

    def lookup_user_did(self, platform: str, platform_user_id: str) -> str | None:
        """Read-only lookup. Checks LRU cache first; queries SQLite on miss."""
        key = (platform, platform_user_id)

        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]

        with self._conn_lock:
            row = self._conn.execute(
                "SELECT user_did FROM user_identity_links "
                "WHERE platform = ? AND platform_user_id = ?",
                (platform, platform_user_id),
            ).fetchone()

        if row is None:
            return None

        user_did: str = row[0]

        with self._cache_lock:
            self._cache[key] = user_did
            self._cache.move_to_end(key)
            if len(self._cache) > _LRU_MAX_SIZE:
                self._cache.popitem(last=False)

        return user_did

    def list_links(self, user_did: str) -> list[Link]:
        """Return all linked identities for a given user_did."""
        with self._conn_lock:
            rows = self._conn.execute(
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
    # Async variants (Wave B2 callers await these)
    # ------------------------------------------------------------------

    async def resolve_user_identity_async(
        self, platform: str, platform_user_id: str
    ) -> str:
        """Async variant of resolve_user_identity."""
        return await asyncio.to_thread(
            self.resolve_user_identity, platform, platform_user_id
        )

    async def lookup_user_did_async(
        self, platform: str, platform_user_id: str
    ) -> str | None:
        """Async variant of lookup_user_did."""
        return await asyncio.to_thread(
            self.lookup_user_did, platform, platform_user_id
        )

    async def link_identities_async(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
        linked_by_did: str,
    ) -> None:
        """Async variant of link_identities."""
        await asyncio.to_thread(
            self.link_identities,
            user_did,
            platform,
            platform_user_id,
            linked_by_did,
        )

    async def unlink_identity_async(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
    ) -> None:
        """Async variant of unlink_identity."""
        await asyncio.to_thread(
            self.unlink_identity, user_did, platform, platform_user_id
        )

    async def list_links_async(self, user_did: str) -> list[Link]:
        """Async variant of list_links."""
        return await asyncio.to_thread(self.list_links, user_did)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        self._conn.executescript(_DDL)
        self._conn.commit()

    def _invalidate_cache(self, platform: str, platform_user_id: str) -> None:
        key = (platform, platform_user_id)
        with self._cache_lock:
            self._cache.pop(key, None)

    def _insert_or_ignore(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
        linked_by_did: str,
    ) -> None:
        linked_at = time.time()
        with self._conn_lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_identity_links "
                "(user_did, platform, platform_user_id, linked_at, linked_by_did) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_did, platform, platform_user_id, linked_at, linked_by_did),
            )
            self._conn.commit()

    def _emit_link_event(
        self,
        user_did: str,
        platform: str,
        platform_user_id: str,
        linked_by_did: str,
    ) -> None:
        if self._telemetry is None:
            return
        self._telemetry.audit_event(
            "gateway.identity.link",
            {
                "user_did": user_did,
                "platform": platform,
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


def _derive_user_did(platform: str, platform_user_id: str) -> str:
    """Deterministically derive a user DID from (platform, platform_user_id)."""
    raw = f"{platform}:{platform_user_id}"
    prefix = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"did:arc:user:human/{prefix}"


def _hash_user_id(platform_user_id: str, chars: int = 16) -> str:
    """Return the first chars hex chars of SHA-256(platform_user_id)."""
    return hashlib.sha256(platform_user_id.encode()).hexdigest()[:chars]
