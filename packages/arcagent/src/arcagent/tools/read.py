"""Read tool -- read file contents from the workspace.

Returns file contents with line numbers in cat -n format.
Supports offset and limit for reading sections of large files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._validation import resolve_workspace_path

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": (
                "Path to the file to read. Relative paths resolve against the workspace root."
            ),
        },
        "offset": {
            "type": "integer",
            "description": "Line number to start reading from (1-based). Defaults to 1.",
        },
        "limit": {
            "type": "integer",
            "description": ("Maximum number of lines to read. Defaults to 0 (read entire file)."),
        },
    },
    "required": ["file_path"],
}


def create_tool(
    workspace: Path,
    *,
    allowed_paths: list[Path] | None = None,
) -> RegisteredTool:
    """Create a workspace-scoped read tool."""
    ws = workspace.resolve()
    _allowed = allowed_paths

    async def execute(
        *,
        file_path: str,
        offset: int = 1,
        limit: int = 0,
        **_kwargs: Any,
    ) -> str:
        """Read a file and return contents with line numbers."""
        resolved = resolve_workspace_path(file_path, ws, allowed_paths=_allowed)

        if not resolved.exists():
            return f"Error: File not found: {file_path}"

        if not resolved.is_file():
            return f"Error: Not a file: {file_path}"

        # Guard against OOM on huge files (10 MB limit)
        file_size = resolved.stat().st_size
        if file_size > _MAX_FILE_SIZE:
            return (
                f"Error: File too large ({file_size:,} bytes, "
                f"limit {_MAX_FILE_SIZE:,}). Use offset/limit to read sections."
            )

        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: File is not valid UTF-8 text: {file_path}"

        lines = text.splitlines()

        # Apply offset (1-based)
        start = max(0, offset - 1)
        if limit > 0:
            lines = lines[start : start + limit]
        else:
            lines = lines[start:]

        # Format with line numbers (cat -n style)
        numbered = [f"{i:>6}\t{line}" for i, line in enumerate(lines, start=start + 1)]
        return "\n".join(numbered)

    return RegisteredTool(
        name="read",
        description=("Read a file from the workspace. Returns contents with line numbers."),
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.read",
    )
