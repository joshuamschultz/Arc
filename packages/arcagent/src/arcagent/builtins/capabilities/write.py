"""Built-in ``write`` tool — SPEC-021 port of arcagent.tools.write.

Writes ``content`` to ``file_path`` inside the workspace, creating
parent directories as needed. Overwrites existing files.
"""

from __future__ import annotations

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool
from arcagent.tools._validation import resolve_workspace_path


@tool(
    name="write",
    description=(
        "Write content to a file in the workspace. "
        "Creates parent directories as needed. Overwrites existing files."
    ),
    classification="state_modifying",
    capability_tags=["file_write"],
    when_to_use="When you need to create or overwrite a workspace file.",
    version="1.0.0",
)
async def write(file_path: str, content: str) -> str:
    """Write ``content`` to ``file_path`` and return a one-line summary."""
    resolved = resolve_workspace_path(
        file_path,
        _runtime.workspace(),
        allowed_paths=_runtime.allowed_paths(),
    )
    if resolved.exists() and not resolved.is_file():
        return f"Error: Not a file: {file_path}"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    byte_count = len(content.encode("utf-8"))
    return f"Written {byte_count} bytes to {file_path}"
