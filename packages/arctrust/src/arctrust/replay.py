"""Team-agnostic sliding-window replay protection."""

from __future__ import annotations

from datetime import UTC, datetime


class ReplayCache:
    """Sliding-window nonce cache. Rejects replays and stale timestamps."""

    def __init__(self, window_seconds: float = 300.0) -> None:
        self._window = window_seconds
        self._seen: dict[str, datetime] = {}

    def _prune(self, now: datetime) -> None:
        cutoff = now.timestamp() - self._window
        expired = [n for n, t in self._seen.items() if t.timestamp() < cutoff]
        for nonce in expired:
            del self._seen[nonce]

    def check_and_record(self, nonce: str, ts: str) -> bool:
        """Return True if first-seen and fresh; False for replay or stale ``ts``."""
        now = datetime.now(UTC)
        self._prune(now)
        try:
            sent_at = datetime.fromisoformat(ts)
        except ValueError:
            return False
        if now.timestamp() - sent_at.timestamp() > self._window:
            return False
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True
