"""Slash-command primitives — the surface every command implements.

A command is a small object with metadata (``name``, ``aliases``,
``description``) and an async ``handle`` that returns reply text (or ``None``
if it handled its own output). Keeping ``handle`` pure — it returns a string,
the registry sends it — means commands are unit-testable with no adapter and
no live gateway.

Cross-surface by construction: every platform (web, Telegram, Slack via
re-injection, future arctui) delivers a leading-``/`` message as
``InboundEvent.message``, so one registry in ``SessionRouter.handle`` serves
them all with no per-surface code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from arcgateway.executor import InboundEvent
    from arcgateway.session import SessionRouter


@dataclass(frozen=True)
class CommandContext:
    """Everything a command needs to act, resolved once by the registry.

    ``router`` is included so a command can mutate session state (``/new``
    rotates via ``router.new_session``) without reaching back through globals.
    """

    event: InboundEvent
    agent_did: str
    user_did: str
    args: str
    router: SessionRouter


@runtime_checkable
class SlashCommand(Protocol):
    """Structural contract for a slash command.

    ``required_role`` is the seam for future per-command authorization
    (operator-only commands): the registry can gate on it before calling
    ``handle``. Default ``None`` = available to any paired user. Not enforced
    yet — YAGNI — but the field keeps the contract stable when it is.
    """

    name: str
    aliases: tuple[str, ...]
    description: str
    required_role: str | None

    async def handle(self, ctx: CommandContext) -> str | None:
        """Run the command; return reply text or ``None`` if self-handled."""
        ...


@dataclass(frozen=True)
class CommandSpec:
    """One command's name and one-line description, for menus and autocomplete.

    The unit every surface's ``/`` menu is built from — the built-in commands
    and the deployment's workflows alike — so Telegram's ``setMyCommands``,
    Slack's manifest, and arcui's autocomplete all read one list.
    """

    name: str
    description: str


@runtime_checkable
class WorkflowProvider(Protocol):
    """Turns the deployment's workflows into runnable slash commands.

    A workflow ``id`` (a bare name like ``briefing``) becomes ``/briefing``.
    ``specs`` is read live so a workflow created after boot still appears in
    arcui's autocomplete (the Telegram/Slack menus are a boot-time snapshot).
    ``run`` fires the workflow deterministically — bypassing the LLM — and
    returns the line to reply to the user.
    """

    def specs(self) -> list[CommandSpec]:
        """The runnable workflows as ``(name, description)`` specs."""
        ...

    async def run(self, workflow_id: str, *, actor_did: str, args: str) -> str:
        """Start ``workflow_id``; return the reply text (run id / error)."""
        ...
