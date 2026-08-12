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

    Two properties of a live run are tracked separately because they answer
    different questions:

    * ``active_runs`` — every run in flight, whatever started it. This is what
      the operator kill-switch cancels and what the run watcher reads.
    * the injection-target registry — only runs opened by an inbound message.
      Background work (a schedule, a consolidation pass) must never receive an
      injection, and must never be cut short by one, even when it happens to be
      registered under the session key a human is talking on.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        self.active_runs: dict[str, arcrun.RunHandle] = {}
        self._injection_targets: dict[str, arcrun.RunHandle] = {}

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

    @asynccontextmanager
    async def delivery(self, session_key: str) -> AsyncIterator[None]:
        """Serialize the delivery decision for one session.

        Deciding whether a message joins the live run or opens a new one is a
        read followed by an act, and the act suspends. Without this lock two
        messages arriving in the same event-loop tick both read "idle" and both
        open a turn — the pre-await race (Hermes PR #4926) — producing two
        replies and two interleaved histories. Held across the read *and* the
        registration that follows, so the second message always observes the
        first message's run.
        """
        async with self._delivery_locks.setdefault(session_key, asyncio.Lock()):
            yield

    def register(
        self, session_key: str, handle: arcrun.RunHandle, *, interactive: bool
    ) -> None:
        """Track a live run. ``interactive`` runs are the only injection targets.

        A run is interactive when an inbound message opened it. Everything else
        — schedules, consolidation, task dispatch, resumed checkpoints — is
        background: it is registered so it can be observed and cancelled, never
        so a message can be injected into it.
        """
        self.active_runs[session_key] = handle
        if interactive:
            self._injection_targets[session_key] = handle

    def unregister(self, session_key: str, handle: arcrun.RunHandle) -> None:
        """Remove only the registration owned by ``handle``."""
        if self.active_runs.get(session_key) is handle:
            del self.active_runs[session_key]
        if self._injection_targets.get(session_key) is handle:
            del self._injection_targets[session_key]

    def active(self, session_key: str) -> arcrun.RunHandle | None:
        return self.active_runs.get(session_key)

    def injection_target(self, session_key: str) -> arcrun.RunHandle | None:
        """The live run a delivered message may join, or None.

        None while the session is idle *and* while its only run is background
        work — in both cases the message opens its own turn instead.
        """
        return self._injection_targets.get(session_key)

    def clear(self) -> None:
        self.active_runs.clear()
        self._injection_targets.clear()
        self._locks.clear()
        self._delivery_locks.clear()
