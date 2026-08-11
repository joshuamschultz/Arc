"""Built-in ``read`` tool.

Reads a file from the workspace and returns the contents in
``cat -n`` line-numbered form. Supports ``offset`` + ``limit`` for
seeking inside large files. Bounded at 10 MB to prevent OOM.
"""

from __future__ import annotations

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool
from arcagent.tools._secure_workspace_file import SecureWorkspaceFileError, read_regular_file

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


@tool(
    name="read",
    description=("Read a file from the workspace. Returns contents with line numbers."),
    classification="read_only",
    capability_tags=["file_read"],
    when_to_use="When you need to inspect the contents of a workspace file.",
    version="1.0.0",
)
async def read(file_path: str, offset: int = 1, limit: int = 0) -> str:
    """Read a file and return its contents as numbered lines.

    Returns an ``Error: ...`` string on common failures (file missing,
    not a file, too large, non-UTF-8) so the caller can surface the
    failure to the LLM without bubbling exceptions.
    """
    resolved = _runtime.resolve_workspace_path(file_path, tool_name="read")
    try:
        data, _identity = read_regular_file(
            resolved, _runtime.authorized_roots(), max_bytes=_MAX_FILE_SIZE
        )
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except SecureWorkspaceFileError as exc:
        if "exceeds" in str(exc):
            return f"Error: File too large (limit {_MAX_FILE_SIZE:,}). Use offset/limit."
        return f"Error: Not a file: {file_path}"
    except OSError:
        return f"Error: File could not be opened safely: {file_path}"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"Error: File is not valid UTF-8 text: {file_path}"

    lines = text.splitlines()
    start = max(0, offset - 1)
    if limit > 0:
        lines = lines[start : start + limit]
    else:
        lines = lines[start:]
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start=start + 1))
