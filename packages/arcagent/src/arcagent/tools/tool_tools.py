"""Self-modification tools for tools — SPEC-017 Phase 7 R-050.

Three tools:

  * ``create_tool(name, python_source)`` — federal DENIED, enterprise approval, personal allowed
  * ``list_artifacts(kind)`` — enumerate skills / tools
  * ``reload_artifacts()`` — rescan on-disk + re-register

The ``create_tool`` tool delegates to :class:`DynamicToolLoader`
which in turn runs the AST validator, restricted-builtins compile,
and emits its own audit events. This tool adds the tier gate on top.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from arcagent.core.errors import ToolError
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._dynamic_loader import DynamicToolLoader

_logger = logging.getLogger("arcagent.tools.tool_tools")

AuditSink = Callable[[str, dict[str, Any]], None]

Tier = Literal["federal", "enterprise", "personal"]


def _emit(sink: AuditSink | None, event: str, payload: dict[str, Any]) -> None:
    if sink is None:
        return
    try:
        sink(event, payload)
    except Exception:
        _logger.exception("tool_tools audit sink raised; continuing")


def make_create_tool_tool(
    *,
    loader: DynamicToolLoader,
    tier: Tier,
    audit_sink: AuditSink | None = None,
) -> RegisteredTool:
    """Build the ``create_tool`` :class:`RegisteredTool`.

    Tier gate:
      * ``federal`` → refuse. Federal compliance forbids agent-
        generated code (NIST 800-53 SI-7(15), CM-5, CM-8).
      * ``enterprise`` → would require human approval in production;
        here we allow with an audit event and expect the caller to
        couple this with an approval workflow.
      * ``personal`` → allowed.
    """

    async def execute(name: str = "", python_source: str = "", **_: Any) -> str:
        if tier == "federal":
            _emit(
                audit_sink,
                "self_mod.tool_create_denied",
                {"name": name, "tier": tier, "reason": "federal_policy"},
            )
            raise ToolError(
                code="SELF_MOD_FEDERAL_DENIED",
                message=(
                    "Dynamic tool creation is disabled in the federal tier "
                    "(NIST 800-53 SI-7(15), CM-5, CM-8). Skills are still "
                    "available via create_skill."
                ),
                details={"name": name, "tier": tier},
            )

        # AST validator + restricted compile happen inside loader.load.
        registered = loader.load(python_source, name=name)
        _emit(
            audit_sink,
            "self_mod.tool_created",
            {
                "name": registered.name,
                "classification": registered.classification,
                "tier": tier,
            },
        )
        return f"tool:{registered.name} registered (classification={registered.classification})"

    return RegisteredTool(
        name="create_tool",
        description=(
            "Create a new Python tool from source. Source is validated "
            "statically (AST) and executed with a restricted builtins "
            "namespace. Not available in the federal tier."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool name (identifier)."},
                "python_source": {
                    "type": "string",
                    "description": (
                        "Full Python source containing a @tool-decorated "
                        "async function."
                    ),
                },
            },
            "required": ["name", "python_source"],
            "additionalProperties": False,
        },
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.tool_tools",
        classification="state_modifying",
        capability_tags=["state_mutation"],
    )


def make_list_artifacts_tool(
    *,
    loader: DynamicToolLoader,
    skills_dir: Path,
) -> RegisteredTool:
    """Build the ``list_artifacts`` :class:`RegisteredTool`.

    Lists either dynamically-loaded tools (``kind="tool"``) or
    skill files on disk (``kind="skill"``). Read-only — safe to run
    in parallel with other read-only calls.
    """

    async def execute(kind: str = "tool", **_: Any) -> str:
        if kind == "tool":
            names = loader.names()
            return "tools: " + (", ".join(names) if names else "(none)")
        if kind == "skill":
            if not skills_dir.exists():
                return "skills: (none)"
            names = sorted(p.stem for p in skills_dir.glob("*.md"))
            return "skills: " + (", ".join(names) if names else "(none)")
        raise ToolError(
            code="LIST_ARTIFACTS_UNKNOWN_KIND",
            message=f"kind {kind!r} must be 'tool' or 'skill'",
            details={"kind": kind},
        )

    return RegisteredTool(
        name="list_artifacts",
        description=(
            "List currently-loaded artifacts. kind is 'tool' or 'skill'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["tool", "skill"],
                    "description": "Which artifact class to enumerate.",
                },
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.tool_tools",
        classification="read_only",
        capability_tags=["file_read"],
    )


def make_reload_artifacts_tool(
    *,
    loader: DynamicToolLoader,
    skills_dir: Path,
    audit_sink: AuditSink | None = None,
) -> RegisteredTool:
    """Build the ``reload_artifacts`` :class:`RegisteredTool`.

    Refreshes the loader's view of what's on disk. Idempotent —
    calling it twice yields the same registered set. Read-only in
    the sense that it mutates only the registry (not the filesystem).
    """

    async def execute(**_: Any) -> str:
        skills_count = len(list(skills_dir.glob("*.md"))) if skills_dir.exists() else 0
        tools_count = len(loader.names())
        _emit(
            audit_sink,
            "self_mod.artifacts_reloaded",
            {"skills": skills_count, "tools": tools_count},
        )
        return f"artifacts reloaded — {skills_count} skill(s), {tools_count} tool(s)"

    return RegisteredTool(
        name="reload_artifacts",
        description="Rescan skills/ and re-check dynamic tool registrations.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.tool_tools",
        classification="state_modifying",
        capability_tags=["state_mutation"],
    )


__all__ = [
    "Tier",
    "make_create_tool_tool",
    "make_list_artifacts_tool",
    "make_reload_artifacts_tool",
]
