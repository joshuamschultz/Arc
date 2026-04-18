"""Leader election — SPEC-017 R-048.

Only the elected leader runs the :class:`ProactiveEngine` tick loop.
All scheduled actions must be idempotent — we aim for at-least-once
execution, never once-and-only-once.

Three implementations ship here; production pickers live in config:

  * :class:`NoOpLeaderElection` — single-instance deployments
    (personal tier). ``acquire_or_wait`` returns True immediately.
  * :class:`InMemoryElection` — shared-memory stand-in for tests and
    single-process multi-worker deployments.
  * Kubernetes Lease / Redis lock implementations are external
    dependencies and are NOT imported here; they live alongside their
    infrastructure modules and match the :class:`LeaderElection`
    Protocol.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

_logger = logging.getLogger("arcagent.proactive.leader")


@runtime_checkable
class LeaderElection(Protocol):
    """Minimum contract every leader-election backend must satisfy.

    ``acquire_or_wait`` returns ``True`` when the instance has become
    the leader (either immediately or after blocking up to the
    backend's timeout). ``release`` frees the lock so another instance
    can acquire it. ``is_leader`` is a cheap status check used by
    dashboards.
    """

    async def acquire_or_wait(self) -> bool: ...

    async def release(self) -> None: ...

    def is_leader(self) -> bool: ...


class NoOpLeaderElection:
    """Always elected — for single-instance deployments.

    Personal tier defaults to this. Has no coordination cost and no
    failure modes, which is exactly right when there's no peer.
    """

    def __init__(self) -> None:
        self._acquired = False

    async def acquire_or_wait(self) -> bool:
        self._acquired = True
        return True

    async def release(self) -> None:
        self._acquired = False

    def is_leader(self) -> bool:
        return self._acquired


class InMemoryElection:
    """Shared-memory mutual exclusion — test + single-process use.

    Backing store is an :class:`InMemoryElection._Lock` instance
    shared between callers. Each caller has its own ``identity`` so
    we can refuse releases from non-holders.
    """

    class _Lock:
        """Shared state across peer InMemoryElection instances."""

        def __init__(self) -> None:
            self._holder: str | None = None
            self._mutex = asyncio.Lock()

        async def acquire(self, identity: str, timeout: float) -> bool:
            try:
                async with asyncio.timeout(timeout):
                    async with self._mutex:
                        if self._holder is None:
                            self._holder = identity
                            return True
                        # Already held — refuse; caller decides whether
                        # to poll or give up. No queueing here; we want
                        # deterministic behaviour in tests.
                        return False
            except TimeoutError:
                return False

        async def release(self, identity: str) -> None:
            async with self._mutex:
                if self._holder == identity:
                    self._holder = None

        def holder(self) -> str | None:
            return self._holder

    def __init__(self, *, lock: _Lock, identity: str) -> None:
        self._lock = lock
        self._identity = identity

    async def acquire_or_wait(self, timeout: float = 1.0) -> bool:
        """Try to acquire the shared lock. ``False`` if already held.

        The ``timeout`` argument is available for tests that want to
        bound how long ``acquire`` waits on the internal mutex. A
        backend exception is treated as a failed acquisition
        (fail-closed): we never claim leadership if the backend is
        sick.
        """
        try:
            acquired = await self._lock.acquire(self._identity, timeout)
        except Exception:
            _logger.exception("Leader acquire failed — treating as not leader")
            return False
        return acquired

    async def release(self) -> None:
        await self._lock.release(self._identity)

    def is_leader(self) -> bool:
        return self._lock.holder() == self._identity


__all__ = [
    "InMemoryElection",
    "LeaderElection",
    "NoOpLeaderElection",
]
