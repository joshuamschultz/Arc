"""SPEC-017 R-048 — K8s Lease + Redis Lock leader election implementations.

Mocks the external clients (kubernetes client, redis-py) so these
tests run without real infrastructure. The production code paths —
fail-closed on error, lease renewal, safe release — are exercised
directly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestRedisLockElection:
    """RedisLockElection against a mocked async redis-py client."""

    async def test_acquire_succeeds_when_set_returns_true(self) -> None:
        from arcagent.modules.proactive.leader_redis import RedisLockElection

        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        redis.eval = AsyncMock(return_value=1)

        election = RedisLockElection(redis=redis, key="arc:test", identity="host-a")
        assert await election.acquire_or_wait(timeout=0.1) is True
        assert election.is_leader() is True
        await election.release()

    async def test_acquire_fails_when_another_holder_present(self) -> None:
        from arcagent.modules.proactive.leader_redis import RedisLockElection

        redis = MagicMock()
        redis.set = AsyncMock(return_value=False)  # held by someone else

        election = RedisLockElection(redis=redis, key="arc:test", identity="host-a")
        assert await election.acquire_or_wait(timeout=0.5) is False
        assert election.is_leader() is False

    async def test_set_exception_is_fail_closed(self) -> None:
        from arcagent.modules.proactive.leader_redis import RedisLockElection

        redis = MagicMock()
        redis.set = AsyncMock(side_effect=RuntimeError("redis down"))

        election = RedisLockElection(redis=redis, key="arc:test", identity="host-a")
        assert await election.acquire_or_wait(timeout=0.1) is False
        assert election.is_leader() is False

    async def test_release_calls_fence_checked_eval(self) -> None:
        from arcagent.modules.proactive.leader_redis import RedisLockElection

        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        redis.eval = AsyncMock(return_value=1)

        election = RedisLockElection(redis=redis, key="arc:test", identity="host-a")
        await election.acquire_or_wait(timeout=0.1)
        await election.release()

        # Release script + key + fence token were all passed
        redis.eval.assert_awaited()
        call_args = redis.eval.await_args_list[-1]
        assert "redis.call('del'" in call_args.args[0]
        assert call_args.args[2] == "arc:test"
        # fence_token starts with identity
        assert call_args.args[3].startswith("host-a:")

    async def test_ttl_vs_renew_validation(self) -> None:
        """Renew interval must be strictly less than TTL."""
        from arcagent.modules.proactive.leader_redis import RedisLockElection

        with pytest.raises(ValueError, match="renew_interval_s"):
            RedisLockElection(
                redis=MagicMock(),
                key="arc:test",
                identity="host",
                ttl_ms=5_000,
                renew_interval_s=10.0,  # 10s * 1000 > 5000 ms
            )


class TestKubernetesLeaseElection:
    """KubernetesLeaseElection — mocked client because real K8s isn't here."""

    def test_constructor_enforces_lease_vs_renew(self) -> None:
        from arcagent.modules.proactive.leader_k8s import KubernetesLeaseElection

        with pytest.raises(ValueError, match="lease_seconds"):
            KubernetesLeaseElection(
                namespace="arc",
                lease_name="proactive",
                identity="host",
                lease_seconds=10,
                renew_seconds=20,  # must be < lease_seconds
            )

    async def test_acquire_fails_closed_when_client_missing(self) -> None:
        """kubernetes package not installed → ``acquire_or_wait`` returns False."""
        from arcagent.modules.proactive.leader_k8s import KubernetesLeaseElection

        election = KubernetesLeaseElection(
            namespace="arc",
            lease_name="proactive",
            identity="host-a",
        )
        # The real import-failure path — kubernetes package is absent
        # in the test environment, so acquire should fail-closed.
        result = await election.acquire_or_wait(timeout=0.1)
        assert result is False
        assert election.is_leader() is False

    def test_stale_detection(self) -> None:
        """`_holder_is_stale` returns True when renew_time + TTL has elapsed."""
        from datetime import UTC, datetime, timedelta

        from arcagent.modules.proactive.leader_k8s import KubernetesLeaseElection

        now = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)
        # Fake lease whose renew happened 60s ago but TTL is 30s → stale
        stale_lease = MagicMock()
        stale_lease.spec.renew_time = now - timedelta(seconds=60)
        stale_lease.spec.lease_duration_seconds = 30

        fresh_lease = MagicMock()
        fresh_lease.spec.renew_time = now - timedelta(seconds=5)
        fresh_lease.spec.lease_duration_seconds = 30

        assert KubernetesLeaseElection._holder_is_stale(stale_lease, now) is True
        assert KubernetesLeaseElection._holder_is_stale(fresh_lease, now) is False

    def test_missing_renew_time_counts_as_stale(self) -> None:
        from datetime import UTC, datetime

        from arcagent.modules.proactive.leader_k8s import KubernetesLeaseElection

        now = datetime(2026, 4, 18, tzinfo=UTC)
        lease = MagicMock()
        lease.spec.renew_time = None
        lease.spec.lease_duration_seconds = 30
        assert KubernetesLeaseElection._holder_is_stale(lease, now) is True


class TestLeaderElectionProtocolConformance:
    """Both impls satisfy the ``LeaderElection`` Protocol duck-typed."""

    def test_redis_conforms(self) -> None:
        from arcagent.modules.proactive.leader import LeaderElection
        from arcagent.modules.proactive.leader_redis import RedisLockElection

        election = RedisLockElection(redis=MagicMock(), key="k", identity="h")
        assert isinstance(election, LeaderElection)

    def test_k8s_conforms(self) -> None:
        from arcagent.modules.proactive.leader import LeaderElection
        from arcagent.modules.proactive.leader_k8s import KubernetesLeaseElection

        election = KubernetesLeaseElection(namespace="n", lease_name="l", identity="h")
        assert isinstance(election, LeaderElection)


# Silence unused-import diagnostic
_ = asyncio
