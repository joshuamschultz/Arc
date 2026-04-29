"""Built-in ``edit`` tool — SPEC-021 port of arcagent.tools.edit.

Performs an exact ``old_string`` → ``new_string`` substitution in a
file. Refuses to edit when ``old_string`` is empty or appears more
than once and ``replace_all`` is false — the caller must supply more
context to disambiguate.
"""

from __future__ import annotations

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool
from arcagent.tools._validation import resolve_workspace_path


@tool(
    name="edit",
    description=(
        "Perform exact string replacement in a file. "
        "Fails if old_string is not unique unless replace_all is true."
    ),
    classification="state_modifying",
    capability_tags=["file_write"],
    when_to_use="When you need to change a precise span inside an existing file.",
    version="1.0.0",
)
async def edit(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace ``old_string`` with ``new_string`` once (or all times)."""
    if not old_string:
        return "Error: old_string must not be empty"

    resolved = resolve_workspace_path(
        file_path,
        _runtime.workspace(),
        allowed_paths=_runtime.allowed_paths(),
    )
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
