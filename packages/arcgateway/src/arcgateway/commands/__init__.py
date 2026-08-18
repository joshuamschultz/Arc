"""Gateway slash-command framework.

``build_default_registry`` is the single place the standard command set is
assembled. Adding a new command later is two lines: write the command class,
then ``registry.register(MyCommand())`` here.
"""

from __future__ import annotations

from arcgateway.commands.base import CommandContext, CommandSpec, SlashCommand, WorkflowProvider
from arcgateway.commands.help import HelpCommand
from arcgateway.commands.new_session import NewSessionCommand
from arcgateway.commands.registry import CommandRegistry


def build_default_registry() -> CommandRegistry:
    """Assemble the default command set shared by every gateway surface."""
    registry = CommandRegistry()
    registry.register(NewSessionCommand())
    registry.register(HelpCommand(registry))
    return registry


__all__ = [
    "CommandContext",
    "CommandRegistry",
    "CommandSpec",
    "HelpCommand",
    "NewSessionCommand",
    "SlashCommand",
    "WorkflowProvider",
    "build_default_registry",
]
