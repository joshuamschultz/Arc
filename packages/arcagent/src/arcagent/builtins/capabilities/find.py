"""Built-in ``find`` tool — SPEC-021 port of arcagent.tools.find.

Glob-match workspace files. Returns paths sorted newest-mtime first
(for "what changed recently?" queries), capped at ``max_results``.
"""

from __future__ import annotations

import stat as stat_module

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool
from arcagent.tools._validation import resolve_workspace_path

_DEFAULT_MAX_RESULTS = 200


@tool(
    name="find",
    description=(
        "Find files by glob pattern. Returns paths sorted by modification time (newest first)."
    ),
    classification="read_only",
    capability_tags=["file_read"],
    when_to_use="When you need to locate files by name pattern in the workspace.",
    version="1.0.0",
)
async def find(
    pattern: str,
    path: str = "",
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> str:
    """Glob-match files; return relative paths sorted by mtime descending."""
    ws = _runtime.workspace()
    allowed = _runtime.allowed_paths()
    search_root = resolve_workspace_path(path, ws, allowed_paths=allowed) if path else ws
    if not search_root.is_dir():
        return f"Error: Not a directory: {path}"
    if ".." in pattern:
        return "Error: Pattern must not contain '..'"

    file_mtimes: list[tuple[object, float]] = []
    for p in search_root.glob(pattern):
        try:
            st = p.stat()
        except OSError:
            continue
        if not stat_module.S_ISREG(st.st_mode):
            continue
        file_mtimes.append((p, st.st_mtime))

    if not file_mtimes:
        return "No matches found."
    file_mtimes.sort(key=lambda x: x[1], reverse=True)
    truncated = len(file_mtimes) > max_results
    file_mtimes = file_mtimes[:max_results]
    lines = [str(p.relative_to(ws)) for p, _ in file_mtimes]  # type: ignore[attr-defined]
    result = "\n".join(lines)
    if truncated:
        result += f"\n(truncated at {max_results} results)"
    return result
