"""Built-in tools for ArcAgent.

Every agent gets these tools by default: read, write, edit, bash.
Tools are workspace-scoped -- all file paths are validated to stay
within the agent's designated workspace boundary.
"""

from __future__ import annotations

from pathlib import Path

from arcagent.core.tool_registry import RegisteredTool


def create_builtin_tools(
    workspace: Path,
    *,
    allowed_paths: list[Path] | None = None,
) -> list[RegisteredTool]:
    """Create all built-in tools scoped to the given workspace.

    Args:
        workspace: The workspace root directory.
        allowed_paths: Additional directories permitted beyond the
            workspace boundary (from tools.policy.allowed_paths config).

    Returns a list of RegisteredTool instances ready for registration.
    """
    from arcagent.tools.bash import create_tool as _bash
    from arcagent.tools.edit import create_tool as _edit
    from arcagent.tools.find import create_tool as _find
    from arcagent.tools.grep import create_tool as _grep
    from arcagent.tools.ls import create_tool as _ls
    from arcagent.tools.read import create_tool as _read
    from arcagent.tools.write import create_tool as _write

    return [
        _read(workspace, allowed_paths=allowed_paths),
        _write(workspace, allowed_paths=allowed_paths),
        _edit(workspace, allowed_paths=allowed_paths),
        _bash(workspace),
        _grep(workspace, allowed_paths=allowed_paths),
        _find(workspace, allowed_paths=allowed_paths),
        _ls(workspace, allowed_paths=allowed_paths),
    ]
