"""Ls tool -- list directory contents.

Returns directory entries with type indicators (d for directory, f for file).
Workspace-scoped. Directories listed before files, both sorted alphabetically.
"""

from __future__ import annotations

import stat as stat_module
from pathlib import Path
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._validation import resolve_workspace_path

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": ("Directory to list. Defaults to the workspace root."),
        },
    },
}


def _format_size(size: int) -> str:
    """Format file size in human-readable form."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def create_tool(
    workspace: Path,
    *,
    allowed_paths: list[Path] | None = None,
) -> RegisteredTool:
    """Create a workspace-scoped ls tool."""
    ws = workspace.resolve()
    _allowed = allowed_paths

    async def execute(
        *,
        path: str = "",
        **_kwargs: Any,
    ) -> str:
        """List directory contents."""
        if path:
            target = resolve_workspace_path(path, ws, allowed_paths=_allowed)
        else:
            target = ws

        if not target.exists():
            return f"Error: Path not found: {path or '.'}"

        if not target.is_dir():
            return f"Error: Not a directory: {path}"

        entries = list(target.iterdir())
        if not entries:
            return "(empty directory)"

        # Single pass: classify entries using the stat result directly
        # (avoids redundant syscalls from is_dir/is_file after stat)
        dirs: list[str] = []
        files: list[tuple[str, int]] = []
        for entry in entries:
            try:
                st = entry.stat()
            except OSError:
                continue
            if stat_module.S_ISDIR(st.st_mode):
                dirs.append(entry.name)
            elif stat_module.S_ISREG(st.st_mode):
                files.append((entry.name, st.st_size))

        dirs.sort()
        files.sort(key=lambda x: x[0])

        lines: list[str] = []
        for name in dirs:
            lines.append(f" d  {name}/")
        for name, size in files:
            lines.append(f" f  {name} ({_format_size(size)})")

        return "\n".join(lines)

    return RegisteredTool(
        name="ls",
        description=(
            "List directory contents with type indicators. "
            "Directories shown first, then files with sizes."
        ),
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.ls",
    )
