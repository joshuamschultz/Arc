"""Connection health tracking for terminal connected-data sync failures.

A terminal failure — a revoked or expired credential — is not a transient blip a
retry clears: retrying just hammers a dead credential every monitor tick, floods
the audit log, and never tells the operator. This module records which
connections need a human before they are synced again, backs them off the timer,
and reports whether a needs-attention notification has already been sent so the
operator is told exactly once per outage.

Backed off is not latched. A credential can come back without anyone acting
inside Arc — a CLI connector's binary holds its own token, and re-signing it in
changes nothing Arc can see — so a backed-off source is allowed ONE recheck run
per ``recheck_after`` seconds. A recheck that fails again re-backs it off without
notifying again; one that succeeds clears it. That is one call an hour against a
dead credential, not one per tick (REQ-427), and it is what stops a reconnected
account from staying silent forever.

The record is in-memory, but the fact is durable elsewhere: the coordinator
persists the terminal ``error_code`` into ``arcstore``'s ``SourceSyncState``, and
the service seeds this tracker from that durable code on inspection, so a restart
re-establishes the backoff rather than resuming hammering.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from arcagent.extension.source import SourceFailureCode

#: Sync failure codes only a human can clear. A source that hits one is backed
#: off until an operator explicitly acts (resume / reindex / sync-now / revoke).
TERMINAL_SYNC_CODES = frozenset({SourceFailureCode.AUTH_REQUIRED.value})


def is_terminal_sync_failure(code: str | None) -> bool:
    """True when ``code`` names a failure that re-syncing cannot fix."""
    return code is not None and code in TERMINAL_SYNC_CODES


class ConnectionHealthTracker:
    """In-memory record of connections that need a human before re-syncing.

    Args:
        recheck_after: Seconds a backed-off source waits before one recheck run.
        clock: Monotonic seconds; injectable so a test does not sleep an hour.
    """

    def __init__(
        self, *, recheck_after: float = 3600.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._backed_off: dict[str, float] = {}
        self._notified: set[str] = set()
        self._recheck_after = recheck_after
        self._clock = clock

    def is_backed_off(self, connection_id: str) -> bool:
        """True while this source must not be re-synced — until an operator acts or
        its recheck window has passed."""
        since = self._backed_off.get(connection_id)
        return since is not None and self._clock() - since < self._recheck_after

    def note_terminal_failure(self, connection_id: str) -> bool:
        """Back the source off from now; return True the first time (so notify once)."""
        self._backed_off[connection_id] = self._clock()
        if connection_id in self._notified:
            return False
        self._notified.add(connection_id)
        return True

    def clear(self, connection_id: str) -> None:
        """An operator acted or a sync succeeded — allow syncing again."""
        self._backed_off.pop(connection_id, None)
        self._notified.discard(connection_id)


__all__ = [
    "TERMINAL_SYNC_CODES",
    "ConnectionHealthTracker",
    "is_terminal_sync_failure",
]
