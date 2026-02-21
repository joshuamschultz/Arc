"""Grep tool -- search file contents by regex pattern.

Returns matching lines with file:line format. Workspace-scoped.
Skips binary files and enforces size limits to prevent OOM.
"""

from __future__ import annotations

import re
import stat as stat_module
from pathlib import Path
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._validation import resolve_workspace_path

_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
_BINARY_CHECK_SIZE = 8192  # Check first 8KB for null bytes
_DEFAULT_MAX_RESULTS = 100
_MAX_PATTERN_LENGTH = 1000  # Prevent ReDoS via long/complex patterns

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Regex pattern to search for in file contents.",
        },
        "path": {
            "type": "string",
            "description": ("File or directory to search. Defaults to the workspace root."),
        },
        "glob_filter": {
            "type": "string",
            "description": (
                "Glob pattern to filter files (e.g., '*.py'). "
                "Only files matching this glob are searched."
            ),
        },
        "max_results": {
            "type": "integer",
            "description": ("Maximum number of matching lines to return. Defaults to 100."),
        },
    },
    "required": ["pattern"],
}


def _is_binary_prefix(data: bytes) -> bool:
    """Check if data prefix contains null bytes (binary indicator)."""
    return b"\x00" in data[:_BINARY_CHECK_SIZE]


def create_tool(
    workspace: Path,
    *,
    allowed_paths: list[Path] | None = None,
) -> RegisteredTool:
    """Create a workspace-scoped grep tool."""
    ws = workspace.resolve()
    _allowed = allowed_paths

    async def execute(
        *,
        pattern: str,
        path: str = "",
        glob_filter: str = "",
        max_results: int = _DEFAULT_MAX_RESULTS,
        **_kwargs: Any,
    ) -> str:
        """Search file contents by regex pattern."""
        # Resolve search root
        if path:
            search_root = resolve_workspace_path(path, ws, allowed_paths=_allowed)
        else:
            search_root = ws

        # Guard against ReDoS via excessively long patterns
        if len(pattern) > _MAX_PATTERN_LENGTH:
            return f"Error: Pattern too long ({len(pattern)} chars, max {_MAX_PATTERN_LENGTH})"

        # Compile regex
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Error: Invalid regex pattern: {exc}"

        # Build file iterator (lazy — avoids collecting all paths upfront)
        if search_root.is_file():
            file_iter = iter([search_root])
        elif glob_filter:
            file_iter = search_root.rglob(glob_filter)
        else:
            file_iter = search_root.rglob("*")

        matches: list[str] = []
        for file_path in file_iter:
            try:
                stat = file_path.stat()
            except OSError:
                continue
            if not stat_module.S_ISREG(stat.st_mode):
                continue
            if stat.st_size > _MAX_FILE_SIZE:
                continue

            # Single read: check binary + decode in one pass
            try:
                raw = file_path.read_bytes()
            except OSError:
                continue
            if _is_binary_prefix(raw):
                continue

            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue

            rel = file_path.relative_to(ws)
            for line_num, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{line_num}: {line}")
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

        if not matches:
            return "No matches found."

        result = "\n".join(matches)
        if len(matches) >= max_results:
            result += f"\n(truncated at {max_results} results)"
        return result

    return RegisteredTool(
        name="grep",
        description=(
            "Search file contents by regex pattern. Returns matching lines with file:line format."
        ),
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.grep",
    )
