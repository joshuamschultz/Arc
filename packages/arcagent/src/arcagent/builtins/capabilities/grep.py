"""Built-in ``grep`` tool — SPEC-021 port of arcagent.tools.grep.

Regex-search workspace files. Skips binaries, enforces a 5 MB
per-file size cap, and short-circuits at ``max_results``. Patterns
are length-bounded to defend against ReDoS.
"""

from __future__ import annotations

import re
import stat as stat_module

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool
from arcagent.tools._validation import resolve_workspace_path

_MAX_FILE_SIZE = 5 * 1024 * 1024
_BINARY_CHECK_SIZE = 8192
_DEFAULT_MAX_RESULTS = 100
_MAX_PATTERN_LENGTH = 1000


def _is_binary_prefix(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_CHECK_SIZE]


@tool(
    name="grep",
    description=(
        "Search file contents by regex pattern. Returns matching lines with file:line format."
    ),
    classification="read_only",
    capability_tags=["file_read"],
    when_to_use=("When you need to find lines matching a pattern across workspace files."),
    version="1.0.0",
)
async def grep(
    pattern: str,
    path: str = "",
    glob_filter: str = "",
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> str:
    """Search workspace files for ``pattern``; return ``rel:line: text`` lines."""
    ws = _runtime.workspace()
    allowed = _runtime.allowed_paths()
    search_root = resolve_workspace_path(path, ws, allowed_paths=allowed) if path else ws
    if len(pattern) > _MAX_PATTERN_LENGTH:
        return f"Error: Pattern too long ({len(pattern)} chars, max {_MAX_PATTERN_LENGTH})"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: Invalid regex pattern: {exc}"

    if search_root.is_file():
        file_iter = iter([search_root])
    elif glob_filter:
        file_iter = search_root.rglob(glob_filter)
    else:
        file_iter = search_root.rglob("*")

    matches: list[str] = []
    for file_path in file_iter:
        try:
            st = file_path.stat()
        except OSError:
            continue
        if not stat_module.S_ISREG(st.st_mode):
            continue
        if st.st_size > _MAX_FILE_SIZE:
            continue
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
