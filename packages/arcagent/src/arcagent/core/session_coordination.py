"""Per-session turn serialization and live-run registration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import arcrun


class SessionRunCoordinator:
    """Coordinate complete turns without blocking mid-turn steering.

    A session lock covers history read, execution, and history commit.  The
    active-handle registry remains separately readable so delivery can steer a
    running turn instead of waiting on that lock.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self.active_runs: dict[str, arcrun.RunHandle] = {}

    def _lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    @asynccontextmanager
    async def turn(self, session_key: str) -> AsyncIterator[None]:
        lock = self._lock(session_key)
        async with lock:
            yield

    async def acquire_turn(self, session_key: str) -> None:
        """Reserve a turn whose completion happens in a background finalizer."""
        await self._lock(session_key).acquire()

    def release_turn(self, session_key: str) -> None:
        lock = self._locks.get(session_key)
        if lock is not None and lock.locked():
            lock.release()

    def register(self, session_key: str, handle: arcrun.RunHandle) -> None:
        self.active_runs[session_key] = handle

    def unregister(self, session_key: str, handle: arcrun.RunHandle) -> None:
        """Remove only the registration owned by ``handle``."""
        if self.active_runs.get(session_key) is handle:
            del self.active_runs[session_key]

    def active(self, session_key: str) -> arcrun.RunHandle | None:
        return self.active_runs.get(session_key)

    def clear(self) -> None:
        self.active_runs.clear()
        self._locks.clear()
