"""Write tool -- write content to a file in the workspace.

Creates parent directories as needed. Overwrites existing files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._validation import resolve_workspace_path

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": (
                "Path to the file to write. Relative paths resolve against the workspace root."
            ),
        },
        "content": {
            "type": "string",
            "description": "The content to write to the file.",
        },
    },
    "required": ["file_path", "content"],
}


def create_tool(
    workspace: Path,
    *,
    allowed_paths: list[Path] | None = None,
) -> RegisteredTool:
    """Create a workspace-scoped write tool."""
    ws = workspace.resolve()
    _allowed = allowed_paths

    async def execute(
        *,
        file_path: str,
        content: str,
        **_kwargs: Any,
    ) -> str:
        """Write content to a file, creating parent directories as needed."""
        resolved = resolve_workspace_path(file_path, ws, allowed_paths=_allowed)

        if resolved.exists() and not resolved.is_file():
            return f"Error: Not a file: {file_path}"

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

        byte_count = len(content.encode("utf-8"))
        return f"Written {byte_count} bytes to {file_path}"

    return RegisteredTool(
        name="write",
        description=(
            "Write content to a file in the workspace. "
            "Creates parent directories as needed. Overwrites existing files."
        ),
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.write",
        classification="state_modifying",
        capability_tags=["file_write"],
    )
