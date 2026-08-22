"""Unit tests for the slash-command registry: parse, register, dispatch."""

from __future__ import annotations

from typing import Any, cast

import pytest

from arcgateway.commands import CommandRegistry, build_default_registry
from arcgateway.commands.base import CommandContext, CommandSpec
from arcgateway.executor import InboundEvent


class _RecordingCommand:
    name = "echo"
    aliases: tuple[str, ...] = ("say",)
    description = "Echo the args back."
    required_role: str | None = None

    def __init__(self) -> None:
        self.calls: list[CommandContext] = []

    async def handle(self, ctx: CommandContext) -> str | None:
        self.calls.append(ctx)
        return f"echo:{ctx.args}"


def _event(message: str) -> InboundEvent:
    return InboundEvent(
        platform="web",
        chat_id="c1",
        user_did="did:arc:user:alice",
        agent_did="did:arc:agent:bot",
        session_key="k",
        message=message,
    )


def test_parse_splits_command_and_args() -> None:
    assert CommandRegistry.parse("/new") == ("new", "")
    assert CommandRegistry.parse("/reset now please") == ("reset", "now please")
    assert CommandRegistry.parse("  /New  ") is None  # leading whitespace = not a command
    assert CommandRegistry.parse("hello") is None
    assert CommandRegistry.parse("/") is None
    assert CommandRegistry.parse("/HELP") == ("help", "")  # case-normalised


def test_register_maps_name_and_aliases() -> None:
    reg = CommandRegistry()
    cmd = _RecordingCommand()
    reg.register(cmd)
    assert reg.get("echo") is cmd
    assert reg.get("say") is cmd
    assert reg.get("SAY") is cmd
    assert reg.get("nope") is None


def test_unique_and_names_dedupe_aliases() -> None:
    reg = CommandRegistry()
    reg.register(_RecordingCommand())
    assert reg.names() == ["echo"]  # alias "say" not double-counted
    assert len(reg.unique()) == 1


@pytest.mark.asyncio
async def test_dispatch_runs_registered_command_and_replies() -> None:
    reg = CommandRegistry()
    cmd = _RecordingCommand()
    reg.register(cmd)
    sent: list[str] = []

    async def _reply(text: str) -> None:
        sent.append(text)

    handled = await reg.dispatch(
        _event("/echo hi there"),
        "did:arc:agent:bot",
        "did:arc:user:alice",
        cast(Any, object()),  # router unused by this command
        _reply,
    )

    assert handled is True
    assert sent == ["echo:hi there"]
    assert cmd.calls[0].args == "hi there"


@pytest.mark.asyncio
async def test_dispatch_ignores_unknown_and_non_commands() -> None:
    reg = CommandRegistry()
    reg.register(_RecordingCommand())
    sent: list[str] = []

    # Unknown /token falls through (not handled) — reaches the agent as text.
    assert (
        await reg.dispatch(_event("/unknown x"), "a", "u", cast(Any, object()), sent.append)
        is False
    )
    # Plain text is not a command.
    assert (
        await reg.dispatch(_event("just chatting"), "a", "u", cast(Any, object()), sent.append)
        is False
    )
    assert sent == []


def test_default_registry_has_new_and_help() -> None:
    reg = build_default_registry()
    assert set(reg.names()) == {"new", "help"}
    assert reg.get("reset") is reg.get("new")  # alias


# --- Workflow slash commands --------------------------------------------


class _FakeWorkflowProvider:
    """A stand-in that exposes two workflows and records what was run."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, str, str]] = []

    def specs(self) -> list[CommandSpec]:
        return [
            CommandSpec(name="briefing", description="Run the morning briefing"),
            CommandSpec(name="new", description="a workflow that clashes with /new"),
        ]

    async def run(self, workflow_id: str, *, actor_did: str, args: str) -> str:
        self.ran.append((workflow_id, actor_did, args))
        return f"Started {workflow_id} (run r-123)"


def test_workflow_names_join_the_menu_but_never_shadow_a_builtin() -> None:
    reg = build_default_registry()
    reg.set_workflow_provider(_FakeWorkflowProvider())
    # "briefing" is added; the "new" workflow is dropped — the built-in wins.
    assert "briefing" in reg.names()
    assert reg.names().count("new") == 1
    specs = {s.name: s.description for s in reg.command_specs()}
    assert specs["briefing"] == "Run the morning briefing"
    assert specs["new"] != "a workflow that clashes with /new"  # the built-in's text


@pytest.mark.asyncio
async def test_dispatch_runs_a_workflow_command() -> None:
    reg = build_default_registry()
    provider = _FakeWorkflowProvider()
    reg.set_workflow_provider(provider)
    sent: list[str] = []

    async def _reply(text: str) -> None:
        sent.append(text)

    handled = await reg.dispatch(
        _event("/briefing extra input"),
        "did:arc:agent:bot",
        "did:arc:user:alice",
        cast(Any, object()),
        _reply,
    )

    assert handled is True
    assert sent == ["Started briefing (run r-123)"]
    assert provider.ran == [("briefing", "did:arc:user:alice", "extra input")]


@pytest.mark.asyncio
async def test_a_builtin_wins_a_name_clash_with_a_workflow() -> None:
    # A registered command named "echo" and a workflow named "echo": the
    # built-in must handle /echo, and the workflow must NOT run.
    reg = CommandRegistry()
    cmd = _RecordingCommand()  # name "echo"
    reg.register(cmd)

    class _EchoClashProvider(_FakeWorkflowProvider):
        def specs(self) -> list[CommandSpec]:
            return [CommandSpec(name="echo", description="clashing workflow")]

    provider = _EchoClashProvider()
    reg.set_workflow_provider(provider)
    sent: list[str] = []

    async def _reply(text: str) -> None:
        sent.append(text)

    handled = await reg.dispatch(_event("/echo hi"), "a", "u", cast(Any, object()), _reply)
    assert handled is True
    assert sent == ["echo:hi"]  # the command ran
    assert provider.ran == []  # the workflow did NOT


@pytest.mark.asyncio
async def test_unknown_token_still_falls_through_with_a_provider() -> None:
    reg = build_default_registry()
    reg.set_workflow_provider(_FakeWorkflowProvider())
    sent: list[str] = []

    async def _reply(text: str) -> None:
        sent.append(text)

    handled = await reg.dispatch(_event("/nope hi"), "a", "u", cast(Any, object()), _reply)
    assert handled is False
    assert sent == []
