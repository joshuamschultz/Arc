"""P14-B step 1 — the run view reads the durable node snapshot first.

``Run.node_states`` is what the runner wrote under revision CAS; the task-row
join is only the fallback for a node the snapshot does not name (a run written
before the snapshot existed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.runs import NodeState, RunStore
from arcstore.tasks import TaskStore
from arcteam.workflow.runner import build_workflow_runner, node_task_id
from arctrust import OperatorKey

from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import build_dashboard_plane

SALES = "did:arc:test:sales"

_DEFINITION = """
[workflow]
id = "two-step"
version = 1
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"

[[node]]
id = "deliver"
kind = "agent"
agent = "@sales"
needs = ["collect"]
"""


class _Entity:
    did = SALES


class _Registry:
    async def get(self, handle: str) -> Any:
        return _Entity() if handle == "sales" else None


@pytest.fixture
async def world(tmp_path: Path) -> Any:
    OperatorKey.generate().save(tmp_path / "operator" / "operator.key")
    bundle = tmp_path / "workflows" / "two-step"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_DEFINITION, encoding="utf-8")
    backend = FakeBackend()
    await backend.start()
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=tmp_path / "operator" / "operator.key",
        workspace_root=tmp_path,
        registry=_Registry(),
    )
    yield backend, runner, build_dashboard_plane(runner=runner)
    await backend.stop()


async def test_node_row_prefers_node_states_and_falls_back_to_rows(world: Any) -> None:
    backend, runner, plane = world
    actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")
    tasks = TaskStore(backend)
    run_id = (await plane.run_workflow("two-step", {}, actor=actor)).value["run_id"]
    collect = node_task_id(run_id, "collect", 0)
    await tasks.start_task(collect, SALES)
    await tasks.finish(collect, status="done", resolution="ok", actor_did=SALES, output={"n": 1})
    await runner.advance(run_id)

    # The snapshot names only `collect`, with values no task row carries; the
    # `deliver` entry is absent, as on a run written before snapshots existed.
    runs = RunStore(backend)
    current = await runs.get(run_id)
    assert current is not None and "deliver" in current.node_states
    raw = await backend.mutable_read("runs", run_id)
    assert raw is not None
    raw["node_states"] = {
        "collect": NodeState(
            status="done",
            task_id=collect,
            attempts=4,
            max_attempts=5,
            last_error="retried after a 503",
            started_at="2026-10-01T00:00:00+00:00",
            finished_at="2026-10-01T00:05:00+00:00",
        ).model_dump(mode="json")
    }
    await backend.mutable_write("runs", run_id, raw, actor_did=SALES)

    detail = await plane.get_run(run_id, actor=actor)
    nodes = {n["node_id"]: n for n in detail["nodes"]}
    assert nodes["collect"]["status"] == "done"
    assert nodes["collect"]["attempts"] == 4
    assert nodes["collect"]["max_attempts"] == 5
    assert nodes["collect"]["last_error"] == "retried after a 503"
    assert nodes["collect"]["completed_at"] == "2026-10-01T00:05:00+00:00"
    assert nodes["collect"]["output"] == {"n": 1}, "outputs still come from the row"
    assert nodes["deliver"]["status"] == "pending", "an unnamed node falls back to its row"
    assert nodes["deliver"]["attempts"] == 0


async def test_skipped_node_reason_reaches_the_run_view(world: Any) -> None:
    backend, _, plane = world
    actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")
    run_id = (await plane.run_workflow("two-step", {}, actor=actor)).value["run_id"]
    runs = RunStore(backend)
    current = await runs.get(run_id)
    assert current is not None
    await runs.set_node_states(
        run_id,
        {"deliver": NodeState(status="cancelled", reason="upstream collect failed: boom")},
        actor_did=SALES,
        expected_revision=current.revision,
    )
    detail = await plane.get_run(run_id, actor=actor)
    nodes = {n["node_id"]: n for n in detail["nodes"]}
    assert nodes["deliver"]["status"] == "cancelled"
    assert nodes["deliver"]["reason"] == "upstream collect failed: boom"
