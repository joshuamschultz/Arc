"""Abuse case: an agent writes a workflow, then runs its own unsigned draft.

Authoring is not approval. Through the real factory (real definition store, real
runner, real control plane) an agent- or schedule-initiated run of an unsigned
bundle is refused at every tier and audited as denied. The only unsigned run is
an operator draft test at personal tier, audited with a warning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust import OperatorKey
from arctrust.paths import arc_state, default_operator_key_path, workflows_dir

from arcteam.workflow import parse_definition, validate_definition
from arcteam.workflow.control_plane import WorkflowControlPlane
from arcteam.workflow.runner import build_workflow_runner

AGENT_DID = "did:arc:local:agent/1111aaaa"
WORKFLOW = """
[workflow]
id = "selfwritten"
version = 1
description = "Authored by an agent, never signed."
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"
"""


class _Registry:
    async def get(self, ref: str) -> Any:
        return type("Entity", (), {"did": AGENT_DID})() if ref == "sales" else None


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
async def agent_authored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    key_path = default_operator_key_path(tmp_path)
    OperatorKey.generate().save(key_path)
    bundle = workflows_dir(tmp_path) / "selfwritten"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(WORKFLOW)
    backend = FakeBackend()
    await backend.start()
    yield arc_state(tmp_path), key_path, backend, workflows_dir(tmp_path)
    await backend.stop()


def _plane(env: Any, tier: str) -> tuple[WorkflowControlPlane, _Sink]:
    root, key_path, backend, bundles = env
    sink = _Sink()
    runner = build_workflow_runner(
        tier=tier,
        task_store_backend=backend,
        runner_key_path=key_path,
        workspace_root=root,
        registry=_Registry(),
        audit_sink=sink,
    )

    def _validate(definition: Any, *, pending_files: frozenset[str] = frozenset()) -> Any:
        return validate_definition(
            definition, bundle_root=bundles / definition.id, pending_files=pending_files
        )

    plane = WorkflowControlPlane(
        definitions=runner.definitions,
        parse=lambda document: parse_definition(dict(document)),
        validate=_validate,
        runner=runner,
        runs=runner.runs,
        tier=tier,
        audit_sink=sink,
    )
    return plane, sink


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
@pytest.mark.parametrize("initiator", ["agent", "scheduler", "chat"])
async def test_agent_or_schedule_cannot_run_its_own_unsigned_workflow(
    agent_authored: Any, tier: str, initiator: str
) -> None:
    plane, sink = _plane(agent_authored, tier)

    result = await plane.run("selfwritten", input={}, initiator=initiator, actor_did=AGENT_DID)

    assert not result.ok
    assert result.run is None
    denied = [e for e in sink.events if e.outcome == "denied"]
    assert any(e.extra.get("initiator") == initiator for e in denied)
    assert not any(e.action == "workflow.run.unsigned_draft" for e in sink.events)


async def test_operator_draft_run_is_personal_only_and_audited(agent_authored: Any) -> None:
    plane, sink = _plane(agent_authored, "personal")

    result = await plane.run("selfwritten", input={}, initiator="operator", actor_did="did:op")

    assert result.ok
    assert any(e.action == "workflow.run.unsigned_draft" for e in sink.events)
    plane_ent, _ = _plane(agent_authored, "enterprise")
    refused = await plane_ent.run(
        "selfwritten", input={}, initiator="operator", actor_did="did:op"
    )
    assert not refused.ok
