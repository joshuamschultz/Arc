"""arccli.commands — Centralized slash command registry.

Public API (minimal surface — SDD §3.11, §5):
    CommandDef           — frozen dataclass describing one slash command
    COMMAND_REGISTRY     — authoritative list of all CommandDef instances
    resolve_command      — alias-aware lookup: name -> CommandDef | None
    commands_by_category — group registry by category for help rendering
"""

from arccli.commands.registry import COMMAND_REGISTRY, CommandDef, resolve_command
from arccli.commands.render import commands_by_category

__all__ = [
    "COMMAND_REGISTRY",
    "CommandDef",
    "commands_by_category",
    "resolve_command",
]
