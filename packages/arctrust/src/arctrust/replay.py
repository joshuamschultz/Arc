"""Team-agnostic sliding-window replay protection."""

from __future__ import annotations

from datetime import UTC, datetime


class ReplayCache:
    """Sliding-window nonce cache. Rejects replays and stale timestamps.

    A nonce is remembered until its own timestamp can no longer pass the
    freshness check (``max(arrival, ts) + window``). Pruning by arrival time
    alone would forget a future-stamped nonce while its ``ts`` is still fresh,
    reopening a replay for that request.
    """

    def __init__(self, window_seconds: float = 300.0) -> None:
        self._window = window_seconds
        # nonce -> POSIX time after which the entry may be forgotten.
        self._forget_after: dict[str, float] = {}

    def __len__(self) -> int:
        """Number of nonces currently remembered (memory observability)."""
        return len(self._forget_after)

    def _prune(self, now: float) -> None:
        expired = [n for n, until in self._forget_after.items() if until < now]
        for nonce in expired:
            del self._forget_after[nonce]

    def check_and_record(self, nonce: str, ts: str) -> bool:
        """Return True if first-seen and fresh; False for replay or stale ``ts``."""
        now = datetime.now(UTC).timestamp()
        self._prune(now)
        try:
            sent_at = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return False
        if now - sent_at > self._window:
            return False
        if nonce in self._forget_after:
            return False
        self._forget_after[nonce] = max(now, sent_at) + self._window
        return True
