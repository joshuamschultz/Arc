"""P14-B step 4 — one schedule occurrence is one run, however many times it fires."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from .conftest import Definition, Node, RecordingSink
from .test_runner_frontier import build

# The id the scheduler derives for an occurrence (arcagent scheduler/occurrence.py:
# "schedule_" + sha256 of the occurrence identity). The runner must accept it
# verbatim as the run id, so the signed trigger evidence and the run agree.
_OCCURRENCE_ID = hashlib.sha256(b"arc.schedule.occurrence.v1/nightly/2026-10-02T03:00").hexdigest()
_RUN_ID = "schedule_" + _OCCURRENCE_ID

_ONE_NODE = Definition(id="nightly", nodes=(Node(id="only", kind="agent", agent="@sales"),))
_FIRE: dict[str, Any] = {
    "input": {},
    "initiator": "scheduler",
    "initiator_did": "did:arc:local:agent/1111aaaa",
    "run_id": _RUN_ID,
    "trigger_digest": "digest-of-the-signed-occurrence",
}


async def test_double_fire_same_occurrence_creates_one_run(stores: Any, registry: Any) -> None:
    flow_tasks, _, _ = stores
    sink = RecordingSink()
    runner = build(stores, registry, _ONE_NODE, audit_sink=sink)

    first = await runner.start_run("nightly", **_FIRE)
    second = await runner.start_run("nightly", **_FIRE)

    assert first.run_id == second.run_id == _RUN_ID
    assert flow_tasks.created_keys.count(f"wf/{_RUN_ID}/only/0") == 1
    assert sink.actions().count("workflow.run.started") == 1, "a replay is not a second start"
    assert "workflow.run.start_replayed" in sink.actions()


async def test_concurrent_double_fire_creates_one_run(stores: Any, registry: Any) -> None:
    flow_tasks, runs, _ = stores
    runner = build(stores, registry, _ONE_NODE)

    results = await asyncio.gather(
        runner.start_run("nightly", **_FIRE), runner.start_run("nightly", **_FIRE)
    )

    assert {r.run_id for r in results} == {_RUN_ID}
    assert len(await runs.active_runs()) == 1
    assert flow_tasks.created_keys.count(f"wf/{_RUN_ID}/only/0") == 1
