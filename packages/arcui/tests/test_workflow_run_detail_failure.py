"""P14-A / J3 G2 — the run view says why a run failed and what each node saw and made."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import TaskStore
from arcteam.workflow.runner import build_workflow_runner, node_task_id
from arctrust import OperatorKey

from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import NODE_IO_LIMIT_BYTES, build_dashboard_plane

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
    plane = build_dashboard_plane(runner=runner)
    actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")
    yield runner, plane, actor, TaskStore(backend)
    await backend.stop()


async def _run_to_failure(world: Any, *, collect_output: dict[str, Any]) -> str:
    runner, plane, actor, tasks = world
    started = await plane.run_workflow("two-step", {}, actor=actor)
    run_id = started.value["run_id"]
    collect = node_task_id(run_id, "collect", 0)
    await tasks.start_task(collect, SALES)
    await tasks.finish(
        collect, status="done", resolution="ok", actor_did=SALES, output=collect_output
    )
    await runner.advance(run_id)
    deliver = node_task_id(run_id, "deliver", 0)
    await tasks.start_task(deliver, SALES)
    await tasks.finish(
        deliver,
        status="failed",
        resolution="node failed",
        actor_did=SALES,
        last_error="ArcLLMAPIError: prompt is too long: 1700000 tokens > 1000000",
    )
    await runner.advance(run_id)
    return run_id


async def test_run_detail_shows_failed_node_error_and_outputs(world: Any) -> None:
    _, plane, actor, _ = world
    run_id = await _run_to_failure(world, collect_output={"files": ["a.txt"]})

    detail = await plane.get_run(run_id, actor=actor)

    assert detail["status"] == "failed"
    assert "prompt is too long" in detail["last_error"]
    nodes = {n["node_id"]: n for n in detail["nodes"]}
    assert nodes["deliver"]["status"] == "failed"
    assert "prompt is too long" in nodes["deliver"]["last_error"]
    assert nodes["deliver"]["attempts"] >= 1
    assert nodes["collect"]["output"] == {"files": ["a.txt"]}
    assert nodes["deliver"]["input"]["upstream"]["collect"] == {"files": ["a.txt"]}


async def test_run_detail_bounds_a_huge_node_output(world: Any) -> None:
    _, plane, actor, _ = world
    run_id = await _run_to_failure(world, collect_output={"blob": "y" * 500_000})

    detail = await plane.get_run(run_id, actor=actor)

    out = {n["node_id"]: n for n in detail["nodes"]}["collect"]["output"]
    assert out["truncated"] is True
    assert out["size_bytes"] > 400_000
    assert len(out["preview"]) <= NODE_IO_LIMIT_BYTES
