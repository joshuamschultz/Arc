"""``/help`` — list the registered slash commands.

Holds a reference to its registry so the listing always reflects whatever is
currently registered — no hardcoded command list to drift.
"""

from __future__ import annotations

from arcgateway.commands.base import CommandContext, SlashCommand
from arcgateway.commands.registry import CommandRegistry


class HelpCommand:
    """List available commands and their descriptions."""

    name = "help"
    aliases: tuple[str, ...] = ()
    description = "List available commands."
    required_role: str | None = None

    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry

    async def handle(self, ctx: CommandContext) -> str | None:
        lines = ["Available commands:"]
        commands: list[SlashCommand] = self._registry.unique()
        lines.extend(f"  /{cmd.name} — {cmd.description}" for cmd in commands)
        return "\n".join(lines)
