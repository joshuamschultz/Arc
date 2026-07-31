"""Architecture test: arccli.commands exposes only the minimal public API.

SDD §5: only CommandDef, COMMAND_REGISTRY, resolve_command, commands_by_category
are exported from arccli.commands.
"""

from __future__ import annotations

import importlib
import inspect

ALLOWED_PUBLIC_NAMES = {
    "CommandDef",
    "COMMAND_REGISTRY",
    "resolve_command",
    "commands_by_category",
}


def test_arccli_commands_minimal_surface() -> None:
    """arccli.commands __all__ or public names must only contain the allowed set."""
    mod = importlib.import_module("arccli.commands")

    if hasattr(mod, "__all__"):
        exported = set(mod.__all__)
    else:
        # Infer public names: non-underscore names defined in the module itself
        exported = {
            name
            for name, obj in inspect.getmembers(mod)
            if not name.startswith("_") and not inspect.ismodule(obj)
        }

    extra = exported - ALLOWED_PUBLIC_NAMES
    assert not extra, (
        f"arccli.commands exports more than allowed. "
        f"Extra names: {sorted(extra)}. "
        f"Allowed: {sorted(ALLOWED_PUBLIC_NAMES)}"
    )


def test_commanddef_importable_from_commands() -> None:
    from arccli.commands import CommandDef  # noqa: F401


def test_command_registry_importable_from_commands() -> None:
    from arccli.commands import COMMAND_REGISTRY  # noqa: F401


def test_resolve_command_importable_from_commands() -> None:
    from arccli.commands import resolve_command  # noqa: F401


def test_commands_by_category_importable_from_commands() -> None:
    from arccli.commands import commands_by_category  # noqa: F401
