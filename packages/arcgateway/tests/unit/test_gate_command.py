"""``/gate`` — the chat card that resolves a waiting workflow gate (J3 F8)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.commands import build_default_registry
from arcgateway.commands.base import CommandContext
from arcgateway.commands.gate import GateCommand
from arcgateway.commands.workflow_provider import GatewayWorkflowProvider
from arcgateway.executor import InboundEvent

_USER = "did:arc:local:user/paired01"


def _runner_with_roles(*roles: str) -> SimpleNamespace:
    """The live runner's registry lookup: the ONLY source of the decider's roles."""
    return SimpleNamespace(member_roles=AsyncMock(return_value=frozenset(roles)))


def _ctx(args: str) -> CommandContext:
    event = InboundEvent(
        platform="telegram",
        chat_id="1",
        user_did=_USER,
        agent_did="did:arc:local:agent/one",
        message=f"/gate {args}",
    )
    return CommandContext(
        event=event, agent_did=event.agent_did, user_did=_USER, args=args, router=MagicMock()
    )


async def test_the_paired_users_did_is_the_actor() -> None:
    resolver = SimpleNamespace(resolve_gate=AsyncMock(return_value="done"))

    reply = await GateCommand(resolver).handle(_ctx("task-1 revise tighten the intro"))  # type: ignore[arg-type]

    assert reply == "done"
    resolver.resolve_gate.assert_awaited_once_with(
        "task-1", decision="revise", notes="tighten the intro", actor_did=_USER
    )


@pytest.mark.parametrize("args", ["", "task-1"])
async def test_missing_arguments_show_the_card(args: str) -> None:
    resolver = SimpleNamespace(resolve_gate=AsyncMock())

    reply = await GateCommand(resolver).handle(_ctx(args))  # type: ignore[arg-type]

    assert reply is not None and "approve" in reply and "reject" in reply and "revise" in reply
    resolver.resolve_gate.assert_not_awaited()


async def test_an_unknown_word_resolves_nothing() -> None:
    resolver = SimpleNamespace(resolve_gate=AsyncMock())

    reply = await GateCommand(resolver).handle(_ctx("task-1 maybe"))  # type: ignore[arg-type]

    assert reply is not None and "maybe" in reply
    resolver.resolve_gate.assert_not_awaited()


async def test_the_provider_maps_words_onto_control_plane_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane = SimpleNamespace(
        resolve_gate=AsyncMock(return_value=SimpleNamespace(ok=True, errors=())),
        runner=_runner_with_roles("reviewer"),
    )
    provider = GatewayWorkflowProvider()
    monkeypatch.setattr(provider, "_control_plane", lambda: plane)

    reply = await provider.resolve_gate("t1", decision="reject", notes="no", actor_did=_USER)

    plane.resolve_gate.assert_awaited_once_with(
        "t1",
        decision="fail_run",
        notes="no",
        actor_did=_USER,
        actor_roles=frozenset({"reviewer"}),
    )
    plane.runner.member_roles.assert_awaited_once_with(_USER)
    assert reply == "Gate t1 rejected."


async def test_the_provider_reports_a_refusal_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = SimpleNamespace(ok=False, errors=(SimpleNamespace(error="not a workflow gate"),))
    plane = SimpleNamespace(
        resolve_gate=AsyncMock(return_value=refused), runner=_runner_with_roles()
    )
    provider = GatewayWorkflowProvider()
    monkeypatch.setattr(provider, "_control_plane", lambda: plane)

    reply = await provider.resolve_gate("t1", decision="approve", notes="", actor_did=_USER)

    assert "not a workflow gate" in reply


async def test_no_running_engine_is_a_plain_line(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GatewayWorkflowProvider()
    monkeypatch.setattr(provider, "_control_plane", lambda: None)

    reply = await provider.resolve_gate("t1", decision="approve", notes="", actor_did=_USER)

    assert "aren't running" in reply


async def test_an_unexpected_failure_never_reaches_the_chat_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GatewayWorkflowProvider()

    def _boom() -> Any:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(provider, "_control_plane", _boom)

    reply = await provider.resolve_gate("t1", decision="approve", notes="", actor_did=_USER)

    assert "unexpected error" in reply


def test_the_card_is_not_a_default_command() -> None:
    """Registered by bootstrap next to the workflow provider, never by default."""
    assert build_default_registry().get("gate") is None
