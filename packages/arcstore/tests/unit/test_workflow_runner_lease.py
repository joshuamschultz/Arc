"""Backend-neutral unit coverage for ArcFlow runner lease ownership."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from arcstore.backends.memory import FakeBackend
from arcstore.workflow_lease import WorkflowRunnerLease


async def test_only_one_owner_can_acquire_and_stale_owner_cannot_release() -> None:
    backend = FakeBackend()
    now = datetime.now(UTC)
    gateway = WorkflowRunnerLease(backend, owner_id="gateway", clock=lambda: now)
    cli = WorkflowRunnerLease(backend, owner_id="cli", clock=lambda: now)

    gateway_fence, cli_fence = await asyncio.gather(
        gateway.acquire_or_renew(), cli.acquire_or_renew()
    )
    assert (gateway_fence is None) != (cli_fence is None)
    winner, loser = (gateway, cli) if gateway_fence is not None else (cli, gateway)
    original = gateway_fence or cli_fence
    assert original is not None

    assert await winner.release()
    replacement = await loser.acquire_or_renew()
    assert replacement is not None
    assert replacement.token == original.token + 1
    assert not await winner.release(fence=original)
    assert await loser.is_current(replacement)


async def test_expired_lease_can_be_taken_but_old_fence_is_invalid() -> None:
    backend = FakeBackend()
    now = datetime.now(UTC)
    old = WorkflowRunnerLease(backend, owner_id="old", clock=lambda: now)
    old_fence = await old.acquire_or_renew()
    assert old_fence is not None

    new = WorkflowRunnerLease(
        backend, owner_id="new", clock=lambda: now + timedelta(seconds=61)
    )
    new_fence = await new.acquire_or_renew()
    assert new_fence is not None
    assert new_fence.token > old_fence.token
    assert not await old.is_current(old_fence)
