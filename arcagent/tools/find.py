"""Find tool -- find files by glob pattern.

Returns matching file paths sorted by modification time (newest first).
Workspace-scoped with configurable result limit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._validation import resolve_workspace_path

_DEFAULT_MAX_RESULTS = 200

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern to match files (e.g., '**/*.py').",
        },
        "path": {
            "type": "string",
            "description": ("Directory to search in. Defaults to the workspace root."),
        },
        "max_results": {
            "type": "integer",
            "description": ("Maximum number of files to return. Defaults to 200."),
        },
    },
    "required": ["pattern"],
}


def create_tool(
    workspace: Path,
    *,
    allowed_paths: list[Path] | None = None,
) -> RegisteredTool:
    """Create a workspace-scoped find tool."""
    ws = workspace.resolve()
    _allowed = allowed_paths

    async def execute(
        *,
        pattern: str,
        path: str = "",
        max_results: int = _DEFAULT_MAX_RESULTS,
        **_kwargs: Any,
    ) -> str:
        """Find files by glob pattern."""
        if path:
            search_root = resolve_workspace_path(path, ws, allowed_paths=_allowed)
        else:
            search_root = ws

        if not search_root.is_dir():
            return f"Error: Not a directory: {path}"

        # Reject traversal patterns
        if ".." in pattern:
            return "Error: Pattern must not contain '..'"

        # Collect matching files with mtime (single stat per file)
        file_mtimes: list[tuple[Path, float]] = []
        for p in search_root.glob(pattern):
            try:
                st = p.stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            file_mtimes.append((p, st.st_mtime))

        if not file_mtimes:
            return "No matches found."

        # Sort by mtime descending (newest first)
        file_mtimes.sort(key=lambda x: x[1], reverse=True)

        # Apply limit
        truncated = len(file_mtimes) > max_results
        file_mtimes = file_mtimes[:max_results]

        # Format as relative paths
        lines = [str(p.relative_to(ws)) for p, _ in file_mtimes]
        result = "\n".join(lines)
        if truncated:
            result += f"\n(truncated at {max_results} results)"
        return result

    return RegisteredTool(
        name="find",
        description=(
            "Find files by glob pattern. Returns paths sorted by modification time (newest first)."
        ),
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.find",
    )
