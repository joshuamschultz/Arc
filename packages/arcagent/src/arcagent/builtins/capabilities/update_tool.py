"""Built-in ``update_tool`` — SPEC-021 R-033.

Replaces an existing tool's source and bumps its semver per
``version_bump`` (``"major"`` / ``"minor"`` / ``"patch"``). The new
source must contain a ``version="X.Y.Z"`` argument on the ``@tool``
decorator that matches the bump; the LLM is responsible for keeping
that consistent. We re-validate the new source before writing.
"""

from __future__ import annotations

import re

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool
from arcagent.tools._dynamic_loader import ASTValidationError, AstValidator

_CAPABILITIES_SUBDIR = ".capabilities"
_VERSION_RE = re.compile(r'version\s*=\s*["\']([0-9]+\.[0-9]+\.[0-9]+)["\']')


def _bump(current: str, kind: str) -> str:
    """Return the bumped semver for ``kind`` ('major' | 'minor' | 'patch')."""
    parts = [int(p) for p in current.split(".")]
    if len(parts) != 3:
        raise ValueError(f"version {current!r} is not semver")
    major, minor, patch = parts
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"version_bump must be major/minor/patch, got {kind!r}")


@tool(
    name="update_tool",
    description=(
        "Update an existing @tool source in the workspace, bumping "
        "the semver. Validates AST before writing."
    ),
    classification="state_modifying",
    capability_tags=["self_modification"],
    when_to_use="When you need to change a tool already in workspace/.capabilities/.",
    requires_skill="update-tool",
    version="1.0.0",
)
async def update_tool(
    name: str,
    new_source: str,
    version_bump: str = "patch",
) -> str:
    """Overwrite ``<workspace>/.capabilities/<name>.py`` with ``new_source``.

    Reads the current version from the existing file's ``@tool``
    decorator, bumps it per ``version_bump``, and rejects the update
    if ``new_source`` does not contain the bumped version literal.
    """
    workspace = _runtime.workspace()
    target = workspace / _CAPABILITIES_SUBDIR / f"{name}.py"
    if not target.exists():
        return f"Error: tool {name!r} not found at {target.relative_to(workspace)}"
    current = target.read_text(encoding="utf-8")
    match = _VERSION_RE.search(current)
    if match is None:
        return f"Error: existing {name!r} has no version=... on @tool"
    try:
        new_version = _bump(match.group(1), version_bump)
    except ValueError as exc:
        return f"Error: {exc}"
    if f'version="{new_version}"' not in new_source and (
        f"version='{new_version}'" not in new_source
    ):
        return (
            f'Error: new_source must declare version="{new_version}" '
            f"after a {version_bump} bump from {match.group(1)}"
        )
    try:
        AstValidator().validate(new_source)
    except ASTValidationError as exc:
        return f"Error: AST validation rejected source — {exc}"
    target.write_text(new_source, encoding="utf-8")
    return f"Updated tool {name!r} {match.group(1)} → {new_version}"
