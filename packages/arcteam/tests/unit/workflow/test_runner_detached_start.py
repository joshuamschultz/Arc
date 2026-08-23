"""Detached run creation must not require the runner lease.

The service process owns the singleton lease for as long as it lives, so a
CLI or dashboard `run --detach` that demanded the lease could never start a
run on a live deployment. A detached start only creates the Run row; the
lease-holding runner materializes its frontier on the next tick.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcteam.workflow.runner import WorkflowRunnerLeaseUnavailableError

from .conftest import Definition, Node
from .test_runner_frontier import build

FLOW = Definition(
    id="detached-flow",
    channel="channel://detached",
    nodes=(Node(id="collect", kind="agent", agent="@sales"),),
)


class _LeaseHeldElsewhere:
    """A lease whose owner is another live process; renewal always refuses."""

    fence = None

    async def acquire_or_renew(self) -> None:
        return None

    async def release(self) -> None:
        return None


async def test_attached_start_requires_the_lease(stores: Any, registry: Any) -> None:
    runner = build(stores, registry, FLOW, lease=_LeaseHeldElsewhere())

    with pytest.raises(WorkflowRunnerLeaseUnavailableError):
        await runner.start_run("detached-flow", input={}, initiator_did="did:arc:local:user/9")


async def test_detached_start_creates_the_run_without_the_lease(
    stores: Any, registry: Any
) -> None:
    flow_tasks, runs, _ = stores
    runner = build(stores, registry, FLOW, lease=_LeaseHeldElsewhere())

    run = await runner.start_run(
        "detached-flow", input={}, initiator_did="did:arc:local:user/9", detached=True
    )

    assert run.status == "running"
    # No frontier yet: materialization belongs to the lease owner's next tick.
    assert await flow_tasks.query_by_flow_run(run.run_id) == []
    assert await runs.get(run.run_id) is not None
