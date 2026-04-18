"""SPEC-017 Phase 6 Task 6.16-6.17 — Leader election.

Three implementations:
  * ``NoOpLeaderElection`` — single-instance deployments (personal tier)
  * ``KubernetesLeaseElection`` — production multi-instance via K8s Lease
  * ``RedisLockElection`` — fallback when K8s API isn't available

Tests focus on the Protocol contract and NoOp (the only impl that
doesn't require external infrastructure). K8s / Redis impls are
tested separately via integration fixtures, not in this unit test.
"""

from __future__ import annotations

import asyncio

import pytest


class TestNoOpLeaderElection:
    """Personal tier / single-instance — always elected."""

    async def test_acquire_returns_immediately(self) -> None:
        from arcagent.modules.proactive.leader import NoOpLeaderElection

        election = NoOpLeaderElection()
        acquired = await asyncio.wait_for(election.acquire_or_wait(), timeout=0.5)
        assert acquired is True
        assert election.is_leader() is True

    async def test_release_is_idempotent(self) -> None:
        from arcagent.modules.proactive.leader import NoOpLeaderElection

        election = NoOpLeaderElection()
        await election.acquire_or_wait()
        await election.release()
        await election.release()  # second call must not raise
        assert election.is_leader() is False


class TestLeaderElectionProtocol:
    """A random object with the right shape satisfies the Protocol."""

    def test_protocol_detects_shape(self) -> None:
        from arcagent.modules.proactive.leader import LeaderElection

        class _Custom:
            async def acquire_or_wait(self) -> bool:
                return True

            async def release(self) -> None:
                return None

            def is_leader(self) -> bool:
                return True

        assert isinstance(_Custom(), LeaderElection)

    def test_protocol_rejects_mismatch(self) -> None:
        from arcagent.modules.proactive.leader import LeaderElection

        class _Bad:
            pass

        assert not isinstance(_Bad(), LeaderElection)


class TestInMemoryMultiInstanceElection:
    """Shared-memory stand-in for validating the leader/follower pattern.

    Used by engine integration tests to simulate multi-instance
    without needing K8s or Redis. Exactly one caller wins; the rest
    wait (or give up if timeout elapsed).
    """

    async def test_first_caller_wins(self) -> None:
        from arcagent.modules.proactive.leader import InMemoryElection

        lock = InMemoryElection._Lock()
        a = InMemoryElection(lock=lock, identity="a")
        b = InMemoryElection(lock=lock, identity="b")

        assert await a.acquire_or_wait(timeout=0.1) is True
        assert a.is_leader() is True

        assert await b.acquire_or_wait(timeout=0.1) is False
        assert b.is_leader() is False

    async def test_release_allows_reacquire(self) -> None:
        from arcagent.modules.proactive.leader import InMemoryElection

        lock = InMemoryElection._Lock()
        a = InMemoryElection(lock=lock, identity="a")
        b = InMemoryElection(lock=lock, identity="b")

        await a.acquire_or_wait(timeout=0.1)
        await a.release()
        assert await b.acquire_or_wait(timeout=0.1) is True
        assert b.is_leader() is True

    async def test_only_holder_can_release(self) -> None:
        """Safety: non-holder's release() is a no-op, not a steal."""
        from arcagent.modules.proactive.leader import InMemoryElection

        lock = InMemoryElection._Lock()
        a = InMemoryElection(lock=lock, identity="a")
        b = InMemoryElection(lock=lock, identity="b")

        await a.acquire_or_wait(timeout=0.1)
        await b.release()  # non-holder; must not release a's lock
        assert a.is_leader() is True
        assert await b.acquire_or_wait(timeout=0.1) is False


@pytest.mark.asyncio
async def test_election_fail_closed_on_exception() -> None:
    """If the backend raises, the caller is not elected.

    Guards against a bug where an exception during acquire mistakenly
    returns True (which would run the engine on multiple instances).
    """
    from arcagent.modules.proactive.leader import InMemoryElection

    class _BrokenLock:
        async def acquire(self, identity: str, timeout: float) -> bool:
            raise RuntimeError("backend down")

        async def release(self, identity: str) -> None:
            return None

        def holder(self) -> str | None:
            return None

    election = InMemoryElection(lock=_BrokenLock(), identity="x")  # type: ignore[arg-type]
    assert await election.acquire_or_wait(timeout=0.1) is False
    assert election.is_leader() is False
