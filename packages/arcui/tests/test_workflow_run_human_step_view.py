"""SPEC-088 T-1377 (COMP-016, REQ-617): the run view carries what a person needs to decide.

The arcui gate panel renders a form from the waiting step's schema and shows a
human_input step's prompt. Neither lives anywhere the dashboard can read except
the waiting task row, so the run detail node of a waiting gate or human_input
step carries the row's ``output_schema_resolved`` (the schema object the runner
read from the verified bundle) and, for human_input, its ``prompt``. An agent
node carries neither.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcteam.workflow.runner import build_workflow_runner, node_task_id
from arctrust import OperatorKey

from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import build_dashboard_plane

SALES = "did:arc:test:sales"
APPROVER = "did:arc:test:user/approver"

APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"qty": {"type": "integer"}, "memo": {"type": "string"}},
    "required": ["qty"],
}
PROMISE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"promised": {"type": "string"}},
    "required": ["promised"],
}

_DEFINITION = f"""
[workflow]
id = "human-steps"
version = 1
owner = "@sales"

[[node]]
id = "approve"
kind = "gate"
gate = "human:approve"
approvers = ["{APPROVER}"]
output_schema = "schemas/approval.json"

[[node]]
id = "ask"
kind = "human_input"
prompt = "Confirm the promised date"
needs = ["approve"]
approvers = ["{APPROVER}"]
output_schema = "schemas/promise.json"
"""


class _Entity:
    did = SALES


class _Registry:
    async def get(self, handle: str) -> Any:
        return _Entity() if handle == "sales" else None


@pytest.fixture
async def world(tmp_path: Path) -> Any:
    OperatorKey.generate().save(tmp_path / "operator" / "operator.key")
    bundle = tmp_path / "workflows" / "human-steps"
    (bundle / "schemas").mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_DEFINITION, encoding="utf-8")
    (bundle / "schemas" / "approval.json").write_text(json.dumps(APPROVAL_SCHEMA))
    (bundle / "schemas" / "promise.json").write_text(json.dumps(PROMISE_SCHEMA))
    backend = FakeBackend()
    await backend.start()
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=tmp_path / "operator" / "operator.key",
        workspace_root=tmp_path,
        registry=_Registry(),
    )
    yield runner, build_dashboard_plane(runner=runner)
    await backend.stop()


async def _nodes(plane: Any, run_id: str, actor: OperatorActor) -> dict[str, Any]:
    detail = await plane.get_run(run_id, actor=actor)
    assert detail is not None
    return {n["node_id"]: n for n in detail["nodes"]}


async def test_waiting_gate_node_carries_its_payload_schema(world: Any) -> None:
    _, plane = world
    actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")
    started = await plane.run_workflow("human-steps", {}, actor=actor)
    assert started.value is not None, started.errors
    nodes = await _nodes(plane, started.value["run_id"], actor)

    gate = nodes["approve"]
    assert gate["status"] == "waiting_gate"
    assert gate["kind"] == "gate"
    assert gate["output_schema_resolved"] == APPROVAL_SCHEMA
    assert "prompt" not in gate, "a gate has no prompt"


async def test_waiting_human_input_node_carries_prompt_and_schema(world: Any) -> None:
    runner, plane = world
    actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")
    started = await plane.run_workflow("human-steps", {}, actor=actor)
    assert started.value is not None, started.errors
    run_id = started.value["run_id"]
    resolved = await plane.resolve_gate(
        node_task_id(run_id, "approve", 0),
        decision="approve",
        notes="",
        actor=OperatorActor(did=APPROVER, session_id="s2"),
        payload={"qty": 2},
    )
    assert resolved.errors is None, resolved.errors
    await runner.advance(run_id)
    nodes = await _nodes(plane, run_id, actor)

    ask = nodes["ask"]
    assert ask["status"] == "waiting_gate"
    assert ask["kind"] == "human_input"
    assert ask["prompt"] == "Confirm the promised date"
    assert ask["output_schema_resolved"] == PROMISE_SCHEMA
    # The resolved gate is no longer waiting: nothing to decide, nothing to render.
    assert "output_schema_resolved" not in nodes["approve"]
