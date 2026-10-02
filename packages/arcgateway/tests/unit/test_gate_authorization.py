"""``/gate`` from chat decides only for a gate's approvers (alpha-2 #67).

The chat command, the gateway provider and the REAL arcteam control plane are
wired together here; only the runner's stores are faked. The decider's roles
come from the team registry for the paired user's authenticated DID — never
from anything typed, and pairing alone confers none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcteam.workflow.control_plane import WorkflowControlPlane

from arcgateway.commands import build_default_registry
from arcgateway.commands.base import CommandContext
from arcgateway.commands.gate import GateCommand
from arcgateway.commands.workflow_provider import GatewayWorkflowProvider
from arcgateway.executor import InboundEvent
from arcgateway.session import SessionRouter

GATE = "wf/run-1/review/0"
PAIRED = "did:arc:telegram:4242"
REVIEWER = "did:arc:telegram:7777"
REGISTRY_ROLES = {PAIRED: frozenset({"sales"}), REVIEWER: frozenset({"reviewer"})}


@dataclass
class _Row:
    id: str
    status: str = "review"
    metadata: dict[str, Any] = field(
        default_factory=lambda: {"node_kind": "gate", "flow_run_id": "run-1", "node_id": "review"}
    )


class _Tasks:
    def __init__(self) -> None:
        self.row = _Row(GATE)
        self.writes = 0

    async def get(self, task_id: str) -> _Row | None:
        return self.row if task_id == GATE else None

    async def update_if(
        self, task_id: str, patch: dict[str, Any], *, where: dict[str, Any], actor_did: str
    ) -> _Row | None:
        if self.row.status != where["status"]:
            return None
        self.writes += 1
        self.row.status = patch["status"]
        self.row.metadata = patch["metadata"]
        return self.row


class _Runner:
    """The runner surface the control plane and the provider use."""

    def __init__(self, approvers: tuple[str, ...]) -> None:
        self.tasks = _Tasks()
        self._approvers = approvers
        self.advanced = 0

    async def member_roles(self, did: str) -> frozenset[str]:
        return REGISTRY_ROLES.get(did, frozenset())

    async def gate_approvers(self, task: Any) -> tuple[str, ...]:
        return self._approvers

    async def advance(self, run_id: str) -> None:
        self.advanced += 1


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)


def _wire(
    monkeypatch: pytest.MonkeyPatch, approvers: tuple[str, ...]
) -> tuple[GatewayWorkflowProvider, _Runner, _Sink]:
    runner = _Runner(approvers)
    sink = _Sink()
    plane = WorkflowControlPlane(
        definitions=MagicMock(),
        parse=lambda document: document,
        validate=lambda definition, **_: (),
        runner=runner,  # type: ignore[arg-type]
        runs=MagicMock(),
        tier="personal",
        audit_sink=sink,
    )
    provider = GatewayWorkflowProvider()
    monkeypatch.setattr(provider, "_control_plane", lambda: plane)
    return provider, runner, sink


def _ctx(user_did: str, args: str) -> CommandContext:
    event = InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did=user_did,
        agent_did="did:arc:local:agent/one",
        message=f"/gate {args}",
    )
    return CommandContext(
        event=event, agent_did=event.agent_did, user_did=user_did, args=args, router=MagicMock()
    )


def _denials(sink: _Sink) -> list[Any]:
    return [e for e in sink.events if e.action == "workflow.gate.denied"]


async def test_gate_resolve_refused_for_unlisted_paired_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner, sink = _wire(monkeypatch, ("role:reviewer",))

    reply = await GateCommand(provider).handle(_ctx(PAIRED, f"{GATE} approve"))

    assert reply == f"You aren't an approver for gate {GATE}."
    assert runner.tasks.writes == 0 and runner.advanced == 0
    [denied] = _denials(sink)
    assert denied.actor_did == PAIRED and denied.extra["task_id"] == GATE


async def test_a_paired_user_gets_no_operator_rights_by_pairing_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner, sink = _wire(monkeypatch, ())

    reply = await GateCommand(provider).handle(_ctx(PAIRED, f"{GATE} approve"))

    assert reply is not None and "aren't an approver" in reply
    assert runner.tasks.writes == 0
    assert len(_denials(sink)) == 1


async def test_a_role_typed_into_the_message_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``role:reviewer`` / ``operator`` in the text are notes, never authority."""
    provider, runner, sink = _wire(monkeypatch, ("role:reviewer",))

    reply = await GateCommand(provider).handle(
        _ctx(PAIRED, f"{GATE} approve role:reviewer operator actor_roles=operator")
    )

    assert reply is not None and "aren't an approver" in reply
    assert runner.tasks.writes == 0
    assert len(_denials(sink)) == 1


async def test_a_registered_role_holder_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, runner, _ = _wire(monkeypatch, ("role:reviewer",))

    reply = await GateCommand(provider).handle(_ctx(REVIEWER, f"{GATE} approve"))

    assert reply == f"Gate {GATE} approved."
    assert runner.tasks.row.metadata["gate_actor_did"] == REVIEWER


async def test_a_replayed_approve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, runner, _ = _wire(monkeypatch, ("role:reviewer",))
    command = GateCommand(provider)
    await command.handle(_ctx(REVIEWER, f"{GATE} approve"))

    replay = await command.handle(_ctx(REVIEWER, f"{GATE} approve"))

    assert replay is not None and "already resolved" in replay
    assert runner.tasks.writes == 1 and runner.advanced == 1


async def test_an_unpaired_user_never_reaches_the_gate() -> None:
    resolver = MagicMock()
    resolver.resolve_gate = AsyncMock(return_value="resolved")
    registry = build_default_registry()
    registry.register(GateCommand(resolver))
    store = MagicMock()
    store.is_approved = AsyncMock(return_value=False)
    router = SessionRouter(
        MagicMock(), pairing_store=store, user_allowlist=set(), command_registry=registry
    )
    router._pairing.handle_unpaired_user = AsyncMock()  # type: ignore[method-assign]

    await router.handle(
        InboundEvent(
            platform="telegram",
            chat_id="9",
            user_did="did:arc:telegram:9",
            agent_did="did:arc:local:agent/one",
            message=f"/gate {GATE} approve",
        )
    )

    resolver.resolve_gate.assert_not_awaited()
