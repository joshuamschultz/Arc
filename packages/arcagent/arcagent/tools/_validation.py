"""Shared path validation for workspace-scoped tools.

All file-based tools (read, write, edit) must validate that
resolved paths remain within the agent's workspace boundary.
This prevents directory traversal attacks (e.g., ../../etc/passwd).
"""

from __future__ import annotations

from pathlib import Path

from arcagent.core.errors import ToolError


def resolve_workspace_path(
    file_path: str,
    workspace: Path,
    *,
    allow_symlinks: bool = False,
    allowed_paths: list[Path] | None = None,
) -> Path:
    """Resolve a file path within the workspace boundary.

    Accepts absolute or relative paths. Relative paths are resolved
    against the workspace root. Absolute paths must fall within the
    workspace directory or one of the allowed_paths.

    Args:
        file_path: The path string to resolve.
        workspace: The workspace root directory.
        allow_symlinks: If False (default), reject symlinks.
        allowed_paths: Additional directories that are permitted
            beyond the workspace boundary. Used by tools.policy config.

    Raises:
        ToolError: If the path contains null bytes, is a symlink (when
                   disallowed), or escapes all permitted boundaries.
    """
    # Null byte injection guard
    if "\x00" in file_path:
        raise ToolError(
            code="TOOL_INVALID_PATH",
            message="Path contains null bytes",
            details={"path": repr(file_path)},
        )

    workspace = workspace.resolve()
    candidate = Path(file_path)

    # Build the unresolved path to check for symlinks
    if candidate.is_absolute():
        unresolved = candidate
    else:
        unresolved = workspace / candidate

    # Symlink check: walk all path components within workspace boundary.
    # For absolute paths outside workspace, skip symlink check (the
    # boundary check below will reject them anyway).
    if not allow_symlinks:
        try:
            rel_parts = unresolved.relative_to(workspace).parts
        except ValueError:
            # Path is outside workspace — boundary check will handle it
            rel_parts = ()

        check = workspace
        for part in rel_parts:
            check = check / part
            if check.is_symlink():
                raise ToolError(
                    code="TOOL_SYMLINK_DENIED",
                    message=f"Symlinks are not allowed: {file_path}",
                    details={"path": file_path, "symlink_at": str(check)},
                )

    resolved = unresolved.resolve()

    # Check workspace boundary first
    try:
        resolved.relative_to(workspace)
        return resolved
    except ValueError:
        pass

    # Check allowed_paths boundaries
    if allowed_paths:
        for allowed in allowed_paths:
            try:
                resolved.relative_to(allowed.resolve())
                return resolved
            except ValueError:
                continue

    raise ToolError(
        code="TOOL_PATH_OUTSIDE_WORKSPACE",
        message=f"Path '{file_path}' resolves outside workspace",
        details={"path": file_path, "workspace": str(workspace)},
    )
