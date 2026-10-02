"""Shared gate for the skill-file tools (``read_skill_file``, ``run_skill_script``).

Resolves a skill NAME the model passes to the registered entry the agent is
actually offered (never a path), applies the same federal rule the capability
provider applies (workspace-authored skills are neither offered nor reachable
at federal), and audits every allow/deny once, here.
"""

from __future__ import annotations

from typing import Any

from arcagent.builtins.capabilities import _runtime
from arcagent.capabilities.capability_registry import SkillEntry
from arcagent.core.errors import ToolError

#: Scan-root prefix of agent-authored skills (federal denies them, ADR-023 §3).
_WORKSPACE_ROOT = "workspace"


def offered_skill(skill: str, *, action: str, path: str) -> SkillEntry:
    """Return the offered skill named ``skill`` or refuse (audited)."""
    entry = _runtime.loader().offered_skill(skill)
    if entry is None:
        raise refuse(action, skill, path, "unknown_skill", f"skill {skill!r} is not available")
    if _runtime.tier() == "federal" and entry.scan_root.startswith(_WORKSPACE_ROOT):
        raise refuse(
            action,
            skill,
            path,
            "federal_workspace_skill",
            f"skill {skill!r} is agent-authored and not available at federal tier",
        )
    return entry


def record(action: str, skill: str, path: str, outcome: str, **extra: Any) -> None:
    """Audit one skill-file operation (``skill.file.read`` / ``skill.script.run``)."""
    _runtime.audit(
        action,
        {
            "skill": skill,
            "path": path,
            "outcome": outcome,
            "actor_did": _runtime.caller_did(),
            "tier": _runtime.tier(),
            **extra,
        },
    )


def refuse(action: str, skill: str, path: str, reason: str, message: str) -> ToolError:
    """Audit a denial and return the error to raise (message is model-safe)."""
    record(action, skill, path, "deny", reason=reason)
    return ToolError(
        code="TOOL_SKILL_FILE_REFUSED",
        message=message,
        details={"skill": skill, "path": path, "reason": reason},
    )


__all__ = ["offered_skill", "record", "refuse"]
