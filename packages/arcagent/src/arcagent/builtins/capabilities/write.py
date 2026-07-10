"""Built-in ``write`` tool.

Writes ``content`` to ``file_path`` inside the workspace, creating
parent directories as needed. Overwrites existing files.
"""

from __future__ import annotations

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool


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
    resolved = _runtime.resolve_workspace_path(file_path, tool_name="write")
    _runtime.check_protected(resolved, file_path, tool_name="write")
    _runtime.check_secret_content(content, file_path, tool_name="write")
    if resolved.exists() and not resolved.is_file():
        return f"Error: Not a file: {file_path}"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    resolved.write_text(content, encoding="utf-8")
    message = f"Written {len(encoded)} bytes to {file_path}"
    if _runtime.resign_if_previously_signed(resolved, encoded) is False:
        message += _runtime.audit_unsigned_artifact(resolved, tool_name="write")
    return message
