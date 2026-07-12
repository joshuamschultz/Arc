"""Unit tests for the slash-command registry: parse, register, dispatch."""

from __future__ import annotations

from typing import Any, cast

import pytest

from arcgateway.commands import CommandRegistry, build_default_registry
from arcgateway.commands.base import CommandContext
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
    assert await reg.dispatch(_event("/unknown x"), "a", "u", cast(Any, object()), sent.append) is False
    # Plain text is not a command.
    assert await reg.dispatch(_event("just chatting"), "a", "u", cast(Any, object()), sent.append) is False
    assert sent == []


def test_default_registry_has_new_and_help() -> None:
    reg = build_default_registry()
    assert set(reg.names()) == {"new", "help"}
    assert reg.get("reset") is reg.get("new")  # alias
