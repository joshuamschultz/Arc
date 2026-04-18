"""Self-modification tools for skills — SPEC-017 Phase 7 R-050.

Two tools:

  * ``create_skill(name, markdown_body)`` — write a new skill file
  * ``improve_skill(name, new_markdown_body)`` — replace an existing one

Both enforce path-safe names (no ``..``, no absolute paths, only safe
characters) and emit audit events. These tools are available in every
tier — skills are declarative markdown, not executable code, so the
federal exception for dynamic code does not apply.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcagent.core.errors import ToolError
from arcagent.core.tool_registry import RegisteredTool, ToolTransport

_logger = logging.getLogger("arcagent.tools.skill_tools")

AuditSink = Callable[[str, dict[str, Any]], None]

# Alphanumeric, dash, underscore only. Keeps file paths flat + safe.
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _validate_skill_name(name: str) -> None:
    if not _SAFE_NAME_RE.match(name):
        msg = (
            f"Skill name {name!r} is invalid. Use letters, digits, dash, "
            "or underscore only (max 64 chars, cannot start with dash/underscore)."
        )
        raise ValueError(msg)


def _emit(sink: AuditSink | None, event: str, payload: dict[str, Any]) -> None:
    if sink is None:
        return
    try:
        sink(event, payload)
    except Exception:
        _logger.exception("skill_tools audit sink raised; continuing")


def make_create_skill_tool(
    *,
    skills_dir: Path,
    audit_sink: AuditSink | None = None,
) -> RegisteredTool:
    """Build a ``create_skill`` :class:`RegisteredTool`."""

    async def execute(name: str = "", markdown_body: str = "", **_: Any) -> str:
        _validate_skill_name(name)
        skills_dir.mkdir(parents=True, exist_ok=True)
        target = skills_dir / f"{name}.md"
        target.write_text(markdown_body, encoding="utf-8")
        _emit(
            audit_sink,
            "self_mod.skill_created",
            {"name": name, "bytes": len(markdown_body.encode("utf-8"))},
        )
        return f"skill:{name} written to {target}"

    return RegisteredTool(
        name="create_skill",
        description=(
            "Create a new skill from markdown. Skill files live in the "
            "agent's skills directory and are declarative — no code."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill file name (no extension)."},
                "markdown_body": {
                    "type": "string",
                    "description": "Full markdown contents for the skill.",
                },
            },
            "required": ["name", "markdown_body"],
            "additionalProperties": False,
        },
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.skill_tools",
        classification="state_modifying",
        capability_tags=["file_write", "state_mutation"],
    )


def make_improve_skill_tool(
    *,
    skills_dir: Path,
    audit_sink: AuditSink | None = None,
) -> RegisteredTool:
    """Build an ``improve_skill`` :class:`RegisteredTool`.

    Replaces the full body of an existing skill. Refuses to create a
    new skill — that's ``create_skill``'s job. Keeping the API narrow
    makes intent obvious in the audit trail.
    """

    async def execute(name: str = "", new_markdown_body: str = "", **_: Any) -> str:
        _validate_skill_name(name)
        target = skills_dir / f"{name}.md"
        if not target.exists():
            raise ToolError(
                code="SKILL_NOT_FOUND",
                message=f"Skill {name!r} does not exist; use create_skill to add it",
                details={"name": name, "path": str(target)},
            )
        target.write_text(new_markdown_body, encoding="utf-8")
        _emit(
            audit_sink,
            "self_mod.skill_improved",
            {"name": name, "bytes": len(new_markdown_body.encode("utf-8"))},
        )
        return f"skill:{name} updated at {target}"

    return RegisteredTool(
        name="improve_skill",
        description=(
            "Replace the body of an existing skill with new markdown. "
            "Fails if the skill does not exist."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill file name (no extension)."},
                "new_markdown_body": {
                    "type": "string",
                    "description": "Replacement markdown body.",
                },
            },
            "required": ["name", "new_markdown_body"],
            "additionalProperties": False,
        },
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.skill_tools",
        classification="state_modifying",
        capability_tags=["file_write", "state_mutation"],
    )


__all__ = ["make_create_skill_tool", "make_improve_skill_tool"]
