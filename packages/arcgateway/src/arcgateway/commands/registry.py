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

from arcgateway.commands.base import CommandContext
from arcgateway.telemetry import emit_audit, hash_user_did

if TYPE_CHECKING:
    from arcgateway.commands.base import SlashCommand
    from arcgateway.executor import InboundEvent
    from arcgateway.session import SessionRouter

_logger = logging.getLogger("arcgateway.commands")

# A reply sink: called with the command's response text.
Reply = Callable[[str], Awaitable[None]]


class CommandRegistry:
    """Maps ``/name`` (and aliases) to commands and dispatches inbound events."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        """Register a command under its name and each of its aliases."""
        for token in (command.name, *command.aliases):
            self._commands[token.lower()] = command

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
        """Primary names of all registered commands (Slack manifest wiring)."""
        return [cmd.name for cmd in self.unique()]

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
            return False
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
