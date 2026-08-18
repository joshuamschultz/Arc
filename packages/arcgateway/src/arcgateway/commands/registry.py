"""CommandRegistry — parse a leading ``/token`` and dispatch to a command.

Adding a command is a one-liner (``registry.register(MyCommand())``); the
registry maps the command's ``name`` plus every alias to it. Dispatch returns
``True`` only when a *registered* command handled the message — an unknown
``/token`` returns ``False`` and falls through to the agent as ordinary text
(least surprising for a chat agent where a leading slash may be legitimate
content; discovery is via ``/help``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from arcgateway.commands.base import CommandContext, CommandSpec
from arcgateway.telemetry import emit_audit, hash_user_did

if TYPE_CHECKING:
    from arcgateway.commands.base import SlashCommand, WorkflowProvider
    from arcgateway.executor import InboundEvent
    from arcgateway.session import SessionRouter

_logger = logging.getLogger("arcgateway.commands")

# A reply sink: called with the command's response text.
Reply = Callable[[str], Awaitable[None]]


class CommandRegistry:
    """Maps ``/name`` (and aliases) to commands and dispatches inbound events."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._workflows: WorkflowProvider | None = None

    def register(self, command: SlashCommand) -> None:
        """Register a command under its name and each of its aliases."""
        for token in (command.name, *command.aliases):
            self._commands[token.lower()] = command

    def set_workflow_provider(self, provider: WorkflowProvider | None) -> None:
        """Attach the source that turns workflows into ``/name`` commands.

        Kept separate from static ``register`` because workflows are dynamic —
        created and archived at runtime — so they are resolved live per lookup
        rather than bound once at boot.
        """
        self._workflows = provider

    def _workflow_specs(self) -> list[CommandSpec]:
        """Runnable-workflow specs, or empty when no provider is attached."""
        if self._workflows is None:
            return []
        return self._workflows.specs()

    def get(self, name: str) -> SlashCommand | None:
        """Return the command bound to ``name``/alias, or ``None``."""
        return self._commands.get(name.lower())

    def unique(self) -> list[SlashCommand]:
        """Return each registered command once, in registration order."""
        seen: dict[int, SlashCommand] = {}
        for cmd in self._commands.values():
            seen.setdefault(id(cmd), cmd)
        return list(seen.values())

    def names(self) -> list[str]:
        """Primary names of all commands — built-ins plus workflows."""
        return [spec.name for spec in self.command_specs()]

    def command_specs(self) -> list[CommandSpec]:
        """Every command as a ``(name, description)`` spec, built-ins first.

        The one list every ``/`` menu is built from: Telegram ``setMyCommands``,
        the Slack manifest, and arcui's autocomplete. Built-ins lead so a
        workflow can never shadow ``/new`` or ``/help`` in the menu, matching
        dispatch order (a static command always wins a name clash).
        """
        builtin = [
            CommandSpec(name=cmd.name, description=cmd.description) for cmd in self.unique()
        ]
        reserved = {spec.name for spec in builtin}
        workflows = [spec for spec in self._workflow_specs() if spec.name not in reserved]
        return builtin + workflows

    @staticmethod
    def parse(message: str) -> tuple[str, str] | None:
        """Split ``"/foo bar baz"`` into ``("foo", "bar baz")``.

        Returns ``None`` when the message is not a slash command.
        """
        if not message.startswith("/"):
            return None
        head, _, rest = message[1:].strip().partition(" ")
        if not head:
            return None
        return head.lower(), rest.strip()

    async def dispatch(
        self,
        event: InboundEvent,
        agent_did: str,
        user_did: str,
        router: SessionRouter,
        reply: Reply,
    ) -> bool:
        """Handle ``event`` if it is a registered command.

        Returns ``True`` when a command matched and ran (caller must stop
        routing), ``False`` otherwise (message is ordinary text).
        """
        parsed = self.parse(event.message)
        if parsed is None:
            return False
        name, args = parsed
        command = self._commands.get(name)
        if command is None:
            # Not a built-in — a workflow of that name runs deterministically,
            # bypassing the LLM (the "force it" path). An unknown token is not a
            # workflow either, so it falls through to the agent as ordinary text.
            return await self._dispatch_workflow(name, args, event, user_did, reply)
        emit_audit(
            _logger,
            "gateway.command.dispatched",
            {"command": name, "platform": event.platform, "uid_h": hash_user_did(user_did)},
        )
        ctx = CommandContext(
            event=event,
            agent_did=agent_did,
            user_did=user_did,
            args=args,
            router=router,
        )
        response = await command.handle(ctx)
        if response is not None:
            await reply(response)
        return True

    async def _dispatch_workflow(
        self,
        name: str,
        args: str,
        event: InboundEvent,
        user_did: str,
        reply: Reply,
    ) -> bool:
        """Run ``/name`` as a workflow if one exists; else leave it as text."""
        if self._workflows is None or name not in {s.name for s in self._workflows.specs()}:
            return False
        emit_audit(
            _logger,
            "gateway.command.workflow",
            {"workflow": name, "platform": event.platform, "uid_h": hash_user_did(user_did)},
        )
        response = await self._workflows.run(name, actor_did=user_did, args=args)
        await reply(response)
        return True
