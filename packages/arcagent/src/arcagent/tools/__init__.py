"""Tool transport primitives.

Built-in tools (read, write, edit, bash, grep, find, ls) and the self-
modification tools (reload, create_tool, create_skill, update_tool,
update_skill) live under :mod:`arcagent.builtins.capabilities` and are
discovered by the SPEC-021 :class:`CapabilityLoader`.

This module retains transport primitives (``RegisteredTool``,
``ToolTransport``) for the registry layer.
"""
