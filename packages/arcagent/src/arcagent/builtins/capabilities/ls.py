"""Built-in ``ls`` tool — SPEC-021 port of arcagent.tools.ls.

Lists directory contents with one entry per line. Directories first
(``d  name/``), then files (``f  name (size)``), each sub-list sorted
alphabetically.
"""

from __future__ import annotations

import stat as stat_module

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool
from arcagent.tools._validation import resolve_workspace_path


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@tool(
    name="ls",
    description=(
        "List directory contents with type indicators. "
        "Directories shown first, then files with sizes."
    ),
    classification="read_only",
    capability_tags=["file_read"],
    when_to_use="When you need to enumerate the contents of a workspace folder.",
    version="1.0.0",
)
async def ls(path: str = "") -> str:
    """List entries under ``path`` (or workspace root)."""
    ws = _runtime.workspace()
    allowed = _runtime.allowed_paths()
    target = resolve_workspace_path(path, ws, allowed_paths=allowed) if path else ws
    if not target.exists():
        return f"Error: Path not found: {path or '.'}"
    if not target.is_dir():
        return f"Error: Not a directory: {path}"
    entries = list(target.iterdir())
    if not entries:
        return "(empty directory)"

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
