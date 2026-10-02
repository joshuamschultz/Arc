"""A waiting gate resolves from the CLI and from a chat card (J3 F8, G7, alpha-2 #67).

Both callers land on ``WorkflowControlPlane.resolve_gate`` and name the deciding
human: the operator's DID from the CLI, the paired user's DID from the chat. The
runner then acts on the decision written on the row. From chat only a listed
approver decides; any other paired user is refused.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcgateway.commands.base import CommandContext
from arcgateway.commands.gate import GateCommand
from arcgateway.executor import InboundEvent
from arcteam.workflow.control_plane import GATE_WORDS
from arcteam.workflow.errors import GateNotAuthorizedError
from arcteam.workflow.runner import node_task_id

from arccli.commands import workflow as wf_cmd
from arccli.commands.workflow import workflow_handler

_GATE_WORKFLOW = """
[workflow]
id = "release"
version = 1
owner = "@sales"

[[node]]
id = "ok"
kind = "gate"
gate = "human:approve_release"
approvers = ["did:arc:local:user/paired01"]
"""

_PAIRED_USER = "did:arc:local:user/paired01"
_OTHER_PAIRED_USER = "did:arc:local:user/paired02"


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    from arcstore.backends.memory import FakeBackend

    backend = FakeBackend()
    monkeypatch.setattr(wf_cmd, "_backend_factory", lambda: backend)

    # The team bus is the one thing absent in a unit run: resolve the owner
    # handle locally, as the registry would, so a run can materialize its gate.
    class _Owners:
        async def get(self, handle: str) -> Any:
            return SimpleNamespace(did="did:arc:local:agent/sales01")

    async def _bindings(arc_dir: Path) -> tuple[Any, Any]:
        return _Owners(), None

    monkeypatch.setattr(wf_cmd, "_team_bindings", _bindings)


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    from arccli.commands.operator import load_operator_key

    load_operator_key(tmp_path)
    source = tmp_path / "src" / "release"
    source.mkdir(parents=True)
    (source / "workflow.toml").write_text(_GATE_WORKFLOW, encoding="utf-8")
    workflow_handler(["create", str(source), "--dir", str(tmp_path)])
    return tmp_path


def _start_run_with_waiting_gate(arc_dir: Path) -> str:
    """Start a detached run and tick once, so the gate row exists in ``review``."""

    async def _go() -> str:
        plane, aclose = await wf_cmd._resolve_control_plane(arc_dir)
        try:
            result = await plane.run(
                "release",
                input={},
                initiator="operator",
                actor_did=wf_cmd._actor_did(arc_dir),
                detached=True,
            )
            assert result.run is not None
            await plane.runner.tick()
            return result.run.run_id
        finally:
            await aclose()

    return asyncio.run(_go())


def _gate_row(arc_dir: Path, run_id: str) -> Any:
    async def _go() -> Any:
        plane, aclose = await wf_cmd._resolve_control_plane(arc_dir)
        try:
            return await plane.runner.tasks.get(node_task_id(run_id, "ok", 0))
        finally:
            await aclose()

    return asyncio.run(_go())


class _PlaneResolver:
    """What the gateway composes at runtime: the same control plane, a chat caller."""

    async def resolve_gate(
        self, task_id: str, *, decision: str, notes: str, actor_did: str
    ) -> str:
        async def _go() -> str:
            plane, aclose = await wf_cmd._resolve_control_plane(self.arc_dir)
            try:
                try:
                    result = await plane.resolve_gate(
                        task_id,
                        decision=GATE_WORDS[decision],
                        notes=notes,
                        actor_did=actor_did,
                        actor_roles=await plane.runner.member_roles(actor_did),
                    )
                except GateNotAuthorizedError:
                    return "not an approver"
                return f"gate {decision} recorded" if result.ok else result.errors[0].error
            finally:
                await aclose()

        return await _go()

    arc_dir: Path


def _chat(args: str, user_did: str = _PAIRED_USER) -> CommandContext:
    event = InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did=user_did,
        agent_did="did:arc:local:agent/one",
        message=f"/gate {args}",
    )
    return CommandContext(
        event=event,
        agent_did=event.agent_did,
        user_did=user_did,
        args=args,
        router=MagicMock(),
    )


def test_gate_resolved_via_cli_and_gateway_card(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # CLI: the operator approves.
    first = _start_run_with_waiting_gate(arc_dir)
    row = _gate_row(arc_dir, first)
    assert row.status == "review"

    workflow_handler(["gate", row.id, "approve", "--notes", "ship it", "--dir", str(arc_dir)])

    settled = _gate_row(arc_dir, first)
    assert settled.metadata["gate_decision"] == "approved"
    assert settled.metadata["gate_notes"] == "ship it"
    assert settled.metadata["gate_actor_did"] == wf_cmd._actor_did(arc_dir)
    assert "approved" in capsys.readouterr().out.lower()

    # Chat card: a paired user the gate does not list is refused; the row waits.
    second = _start_run_with_waiting_gate(arc_dir)
    resolver = _PlaneResolver()
    resolver.arc_dir = arc_dir
    denied = asyncio.run(
        GateCommand(resolver).handle(
            _chat(f"{node_task_id(second, 'ok', 0)} approve", user_did=_OTHER_PAIRED_USER)
        )
    )
    assert denied == "not an approver"
    assert _gate_row(arc_dir, second).status == "review"

    # The listed paired user rejects, and their DID is the actor.
    reply = asyncio.run(
        GateCommand(resolver).handle(_chat(f"{node_task_id(second, 'ok', 0)} reject too risky"))
    )

    rejected = _gate_row(arc_dir, second)
    assert rejected.metadata["gate_decision"] == "rejected"
    assert rejected.metadata["gate_notes"] == "too risky"
    assert rejected.metadata["gate_actor_did"] == _PAIRED_USER
    assert reply == "gate reject recorded"


def test_an_unknown_decision_word_is_refused_by_the_cli(arc_dir: Path) -> None:
    with pytest.raises(SystemExit):
        workflow_handler(["gate", "some-task", "maybe", "--dir", str(arc_dir)])


def test_a_non_gate_task_is_refused_with_a_reason(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        workflow_handler(["gate", "not-a-task", "approve", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert "not a workflow gate" in capsys.readouterr().err


def test_the_card_explains_itself_when_called_without_arguments() -> None:
    reply = asyncio.run(GateCommand(SimpleNamespace()).handle(_chat("")))  # type: ignore[arg-type]

    assert reply is not None and "approve" in reply and "reject" in reply and "revise" in reply
