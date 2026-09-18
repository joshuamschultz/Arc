"""Connection health tracking for terminal connected-data sync failures.

A terminal failure — a revoked or expired credential — is not a transient blip a
retry clears: retrying just hammers a dead credential every monitor tick, floods
the audit log, and never tells the operator. This module records which
connections need a human before they are synced again, backs them off the timer,
and reports whether a needs-attention notification has already been sent so the
operator is told exactly once per outage.

The record is in-memory, but the fact is durable elsewhere: the coordinator
persists the terminal ``error_code`` into ``arcstore``'s ``SourceSyncState``, and
the service seeds this tracker from that durable code on inspection, so a restart
re-establishes the backoff rather than resuming hammering.
"""

from __future__ import annotations

from arcagent.extension.source import SourceFailureCode

#: Sync failure codes only a human can clear. A source that hits one is backed
#: off until an operator explicitly acts (resume / reindex / sync-now / revoke).
TERMINAL_SYNC_CODES = frozenset({SourceFailureCode.AUTH_REQUIRED.value})


def is_terminal_sync_failure(code: str | None) -> bool:
    """True when ``code`` names a failure that re-syncing cannot fix."""
    return code is not None and code in TERMINAL_SYNC_CODES


class ConnectionHealthTracker:
    """In-memory record of connections that need a human before re-syncing."""

    def __init__(self) -> None:
        self._backed_off: set[str] = set()
        self._notified: set[str] = set()

    def is_backed_off(self, connection_id: str) -> bool:
        """True when this source must not be re-synced until an operator acts."""
        return connection_id in self._backed_off

    def note_terminal_failure(self, connection_id: str) -> bool:
        """Back the source off; return True the first time (so notify once)."""
        self._backed_off.add(connection_id)
        if connection_id in self._notified:
            return False
        self._notified.add(connection_id)
        return True

    def clear(self, connection_id: str) -> None:
        """An operator acted or a sync succeeded — allow syncing again."""
        self._backed_off.discard(connection_id)
        self._notified.discard(connection_id)


__all__ = [
    "TERMINAL_SYNC_CODES",
    "ConnectionHealthTracker",
    "is_terminal_sync_failure",
]
