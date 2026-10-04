"""A running attempt proves it is alive: its claim stamps a lease and a beat renews it.

The workflow runner reclaims in-flight nodes after a restart. Without a lease it
could only guess from the wall clock, and a 35-minute node with no timeout was
killed at 900 s. The beat is what lets resume tell a live node from a dead one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from arcstore.tasks import (
    PROCESS_INSTANCE_ID,
    RUN_ENDED_UNFINISHED,
    SERVICE_RESTART_INTERRUPTED,
)

from .test_reliability import _HangingRun, _seed_todo, state  # noqa: F401


async def _wait_for_beat(st: Any, task_id: str, after: str | None) -> str:
    for _ in range(200):
        row = await st.store.get(task_id)
        if row is not None and row.lease_beat_at and row.lease_beat_at != after:
            return row.lease_beat_at
        await asyncio.sleep(0.005)
    raise AssertionError("the running attempt never renewed its lease")


async def test_a_dispatched_attempt_stamps_a_lease_and_keeps_beating(
    state: Any, monkeypatch: Any
) -> None:
    from arcagent.modules.tasks import capabilities
    from arcagent.modules.tasks.capabilities import _dispatch_tick, _reliability_tick

    monkeypatch.setattr(capabilities, "ATTEMPT_LEASE_BEAT_S", 0.01)
    st, identity = state
    run = _HangingRun()
    st.agent_run_fn = run
    await _seed_todo(st, identity, "t1", max_attempts=3)

    dispatch = asyncio.ensure_future(_dispatch_tick())
    await asyncio.wait_for(run.started.wait(), timeout=1)
    claimed = await st.store.get("t1")
    assert claimed is not None and claimed.lease_owner == PROCESS_INSTANCE_ID
    first = await _wait_for_beat(st, "t1", None)
    await _wait_for_beat(st, "t1", first)  # it keeps going while the node runs

    assert await st.store.request_cancel("t1", actor_did="did:arc:test:human/operator")
    await _reliability_tick()
    await asyncio.wait_for(dispatch, timeout=1)
    await asyncio.sleep(0.05)
    settled = await st.store.get("t1")
    assert settled is not None
    stamp = settled.lease_beat_at
    await asyncio.sleep(0.05)
    again = await st.store.get("t1")
    assert again is not None and again.lease_beat_at == stamp, "the beat stops with the run"


async def test_startup_reclaim_names_a_service_restart(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _reliability_tick

    st, identity = state
    await _seed_todo(st, identity, "t1", max_attempts=3)
    await st.store.start_task("t1", identity.did, run_id="r1")

    await _reliability_tick()

    row = await st.store.get("t1")
    assert row is not None and (row.last_error or "").startswith(SERVICE_RESTART_INTERRUPTED)


async def test_steady_state_reclaim_names_an_unfinished_run(state: Any) -> None:
    from arcagent.modules.tasks.capabilities import _reliability_tick

    st, identity = state
    st.reclaim_done = True
    st.config = st.config.model_copy(update={"stuck_reclaim_seconds": 0})
    await _seed_todo(st, identity, "t1", max_attempts=3)
    await st.store.start_task("t1", identity.did, run_id="r1")

    await _reliability_tick()

    row = await st.store.get("t1")
    assert row is not None and (row.last_error or "").startswith(RUN_ENDED_UNFINISHED)
