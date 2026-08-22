"""Real PostgreSQL fencing regressions for mutually exclusive ArcFlow runners."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from arcstore.backends.base import ArcStoreBackend
from arcstore.workflow_lease import WorkflowRunnerLease


async def test_postgres_runner_lease_has_one_winner_and_fences_stale_owner(
    postgres_backend: ArcStoreBackend,
) -> None:
    """Only one process can own a lease; an old token cannot release the new owner."""
    now = datetime.now(UTC)
    first = WorkflowRunnerLease(postgres_backend, owner_id="gateway-a", clock=lambda: now)
    second = WorkflowRunnerLease(postgres_backend, owner_id="cli-b", clock=lambda: now)

    won_first, won_second = await asyncio.gather(
        first.acquire_or_renew(), second.acquire_or_renew()
    )
    assert (won_first is None) != (won_second is None)
    winner = first if won_first is not None else second
    loser = second if won_first is not None else first
    first_fence = won_first or won_second
    assert first_fence is not None

    await winner.release()
    next_fence = await loser.acquire_or_renew()
    assert next_fence is not None
    assert next_fence.token > first_fence.token

    # Replaying an old process's release must not clear the new holder's lease.
    assert not await winner.release(fence=first_fence)
    assert await loser.is_current(next_fence)
    assert await loser.release()


async def test_postgres_runner_lease_can_be_taken_after_expiry(
    postgres_backend: ArcStoreBackend,
) -> None:
    """An expired owner loses authority and the replacement receives a higher fence."""
    now = datetime.now(UTC)
    first = WorkflowRunnerLease(postgres_backend, owner_id="gateway-a", clock=lambda: now)
    fence = await first.acquire_or_renew()
    assert fence is not None

    later = now + timedelta(seconds=61)
    replacement = WorkflowRunnerLease(
        postgres_backend,
        owner_id="cli-b",
        clock=lambda: later,
    )
    replacement_fence = await replacement.acquire_or_renew()

    assert replacement_fence is not None
    assert replacement_fence.token > fence.token
    assert not await first.is_current(fence)
