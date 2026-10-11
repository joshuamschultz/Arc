"""Runner work-event emission (SPEC-088 T-1351, COMP-008 / COMP-012).

REQ-623, REQ-630, D-847. The runner takes an injected ``WorkEventSink`` (any
object with ``append(WorkEvent) -> bool``; ``arcstore.WorkEventLog`` is the
production one) as ``WorkflowRunner(..., work_events=<sink>)``, beside the
narrator. It appends:

* ``gate.waiting`` / ``human_input.waiting`` when such a row materializes;
* ``run.finished`` when the run ends ``done``, ``run.failed`` when ``failed``.

Event ids are deterministic, so ``resume()`` may re-append for rows still
waiting and the log stores each event once. ``agent_did`` is the workflow's
owner agent (the agent ``gates:resolve`` and ``events:subscribe`` are scoped to).
No sink means no emission and no error.

Callback URL contract (smallest one consistent with the SDD). No SDD route
passes a callback to a workflow run, so the run's stored input is the carrier:
when it holds ``{"intake": {"callback_url": "<url>"}}`` -- the same
``intake.callback_url`` shape an intake task row carries in its metadata
(SDD Data Model) -- every work event of that run carries that URL. Anything
else (absent, not a mapping, not a string) yields ``callback_url = None``.
Admission is not the runner's job: CallbackPolicy re-checks the allowlist and
the resolved address before every delivery (COMP-013).

These tests use the real ``WorkEventLog`` over the real ``FakeBackend``, so
"appends nothing new" means the log holds no new row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcstore.work_events import WorkEvent, WorkEventLog

from ._human_steps import (
    ALICE,
    PLANNER,
    Clock,
    Harness,
    approval_flow,
    input_flow,
    single_node_flow,
)
from .conftest import SALES_DID, complete_node, fail_node, task_id

CALLBACK = "https://orders.example.com/arc/callback"


def harness(backend: Any, tmp_path: Path, definition: Any, log: Any | None) -> Harness:
    kwargs = {} if log is None else {"runner_kwargs": {"work_events": log}}
    return Harness(backend=backend, definition=definition, root=tmp_path, clock=Clock(), **kwargs)


async def events(log: WorkEventLog, kind: str | None = None) -> list[WorkEvent]:
    page = await log.read(0, limit=1000)
    return [e for e in page if kind is None or e.kind == kind]


async def test_gate_materialization_appends_one_gate_waiting(backend: Any, tmp_path: Path) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, approval_flow(), log)
    run_id = await h.reach("approve")
    await h.runner.advance(run_id)  # a re-derived materialization must not re-emit

    [event] = await events(log, "gate.waiting")
    assert event.run_id == run_id
    assert event.workflow_id == "orders"
    assert event.node_id == "approve"
    assert event.task_id == task_id(run_id, "approve", 0)
    assert event.agent_did == SALES_DID
    assert event.callback_url is None
    assert await events(log, "human_input.waiting") == []


async def test_human_input_materialization_appends_human_input_waiting(
    backend: Any, tmp_path: Path
) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, input_flow(), log)
    run_id = await h.reach("ask_due")

    [event] = await events(log, "human_input.waiting")
    assert event.node_id == "ask_due"
    assert event.task_id == task_id(run_id, "ask_due", 0)
    assert await events(log, "gate.waiting") == []


async def test_a_run_that_finishes_appends_run_finished(backend: Any, tmp_path: Path) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, single_node_flow(), log)
    run_id = await h.start()

    await complete_node(h.tasks, task_id(run_id, "draft", 0), SALES_DID, {"draft": "v1"})
    await h.runner.advance(run_id)
    await h.runner.advance(run_id)

    assert await h.run_status(run_id) == "done"
    [event] = await events(log, "run.finished")
    assert event.run_id == run_id
    assert event.status == "done"
    assert event.agent_did == SALES_DID
    assert await events(log, "run.failed") == []


async def test_a_run_that_fails_appends_run_failed(backend: Any, tmp_path: Path) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, single_node_flow(), log)
    run_id = await h.start()

    await fail_node(h.tasks, task_id(run_id, "draft", 0), SALES_DID, "boom")
    await h.runner.advance(run_id)

    assert await h.run_status(run_id) == "failed"
    [event] = await events(log, "run.failed")
    assert event.run_id == run_id
    assert event.status == "failed"
    assert await events(log, "run.finished") == []


async def test_a_gate_rejection_appends_run_failed(backend: Any, tmp_path: Path) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, approval_flow(), log)
    run_id = await h.reach("approve")

    result = await h.plane.resolve_gate(
        task_id(run_id, "approve", 0),
        decision="fail_run",
        decider_did=ALICE,
        on_behalf_of=None,
        actor_roles=PLANNER,
    )

    assert result.ok, result.errors
    assert len(await events(log, "run.failed")) == 1


async def test_resume_twice_appends_nothing_new(backend: Any, tmp_path: Path) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, approval_flow(), log)
    run_id = await h.reach("approve")
    before = await events(log)
    assert [e.kind for e in before] == ["gate.waiting"]

    await h.runner.resume()
    await h.runner.resume()

    after = await events(log)
    assert [(e.event_id, e.seq) for e in after] == [(e.event_id, e.seq) for e in before]
    assert run_id == after[0].run_id


async def test_a_restarted_runner_emits_the_waiting_event_it_missed(
    backend: Any, tmp_path: Path
) -> None:
    """The gate materialized under a runner with no sink; the restart catches up."""
    log = WorkEventLog(backend)
    clock = Clock()
    before = Harness(backend=backend, definition=approval_flow(), root=tmp_path, clock=clock)
    run_id = await before.reach("approve")

    after = Harness(
        backend=backend,
        definition=approval_flow(),
        root=tmp_path,
        clock=clock,
        runner_kwargs={"work_events": log},
    )
    await after.runner.resume()
    await after.runner.resume()

    [event] = await events(log, "gate.waiting")
    assert event.run_id == run_id
    assert event.task_id == task_id(run_id, "approve", 0)


async def test_events_carry_the_callback_url_from_the_run_input_intake(
    backend: Any, tmp_path: Path
) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, approval_flow(), log)
    run_id = await h.reach("approve", run_input={"intake": {"callback_url": CALLBACK}})

    [waiting] = await events(log, "gate.waiting")
    assert waiting.callback_url == CALLBACK

    result = await h.plane.resolve_gate(
        task_id(run_id, "approve", 0),
        decision="fail_run",
        decider_did=ALICE,
        on_behalf_of=None,
        actor_roles=PLANNER,
    )
    assert result.ok, result.errors
    [failed] = await events(log, "run.failed")
    assert failed.callback_url == CALLBACK


async def test_a_malformed_intake_value_yields_no_callback_and_no_crash(
    backend: Any, tmp_path: Path
) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, approval_flow(), log)
    await h.reach("approve", run_input={"intake": "https://evil.example.com/"})

    [waiting] = await events(log, "gate.waiting")
    assert waiting.callback_url is None


async def test_no_sink_means_no_emission_and_no_error(backend: Any, tmp_path: Path) -> None:
    """Guard (expected to pass before and after T-1352): the sink stays optional.

    An implementation that made the sink mandatory, or wrote events through
    some other path when none is wired, fails here.
    """
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, single_node_flow(), None)
    run_id = await h.start()
    await complete_node(h.tasks, task_id(run_id, "draft", 0), SALES_DID, {"draft": "v1"})
    await h.runner.advance(run_id)

    assert await h.run_status(run_id) == "done"
    assert await events(log) == []
