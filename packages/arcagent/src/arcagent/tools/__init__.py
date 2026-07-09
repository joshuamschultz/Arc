"""Tool authoring surface + transport primitives.

Built-in tools (read, write, edit, bash, grep, find, ls) and the self-
modification tools (reload, create_tool, create_skill, update_tool,
update_skill) live under :mod:`arcagent.builtins.capabilities` and are
discovered by the SPEC-021 :class:`CapabilityLoader`.

The capability decorators are re-exported here so tool authors import them
from the public path — ``from arcagent.tools import tool, hook,
background_task, capability`` — instead of the private ``._decorator``
module. ``capability_meta(fn)`` reads back the metadata a decorator stamped.

This module also retains transport primitives (``RegisteredTool``,
``ToolTransport``) for the registry layer.
"""

from arcagent.tools._decorator import (
    BackgroundTaskMetadata,
    CapabilityClassMetadata,
    CapabilityMetadata,
    HookMetadata,
    ToolClassification,
    ToolMetadata,
    background_task,
    capability,
    capability_meta,
    hook,
    tool,
)

__all__ = [
    "BackgroundTaskMetadata",
    "CapabilityClassMetadata",
    "CapabilityMetadata",
    "HookMetadata",
    "ToolClassification",
    "ToolMetadata",
    "background_task",
    "capability",
    "capability_meta",
    "hook",
    "tool",
]
