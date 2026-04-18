"""Edit tool -- perform exact string replacement in a file.

Requires old_string to be unique in the file unless replace_all is set.
This prevents accidental edits to unintended locations.
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
                "Path to the file to edit. Relative paths resolve against the workspace root."
            ),
        },
        "old_string": {
            "type": "string",
            "description": "The exact text to find and replace.",
        },
        "new_string": {
            "type": "string",
            "description": "The replacement text.",
        },
        "replace_all": {
            "type": "boolean",
            "description": (
                "Replace all occurrences if true. "
                "Defaults to false (requires old_string to be unique)."
            ),
        },
    },
    "required": ["file_path", "old_string", "new_string"],
}


def create_tool(
    workspace: Path,
    *,
    allowed_paths: list[Path] | None = None,
) -> RegisteredTool:
    """Create a workspace-scoped edit tool."""
    ws = workspace.resolve()
    _allowed = allowed_paths

    async def execute(
        *,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        **_kwargs: Any,
    ) -> str:
        """Replace exact string occurrences in a file."""
        if not old_string:
            return "Error: old_string must not be empty"

        resolved = resolve_workspace_path(file_path, ws, allowed_paths=_allowed)

        if not resolved.exists():
            return f"Error: File not found: {file_path}"

        if not resolved.is_file():
            return f"Error: Not a file: {file_path}"

        try:
            content = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: File is not valid UTF-8 text: {file_path}"

        if old_string not in content:
            return f"Error: old_string not found in {file_path}"

        count = content.count(old_string)

        if not replace_all and count > 1:
            return (
                f"Error: old_string found {count} times in {file_path}. "
                "Provide more context to make it unique, or set replace_all=true."
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replaced = 1

        resolved.write_text(new_content, encoding="utf-8")

        return f"Replaced {replaced} occurrence(s) in {file_path}"

    return RegisteredTool(
        name="edit",
        description=(
            "Perform exact string replacement in a file. "
            "Fails if old_string is not unique unless replace_all is true."
        ),
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.edit",
        classification="state_modifying",
        capability_tags=["file_write"],
    )
