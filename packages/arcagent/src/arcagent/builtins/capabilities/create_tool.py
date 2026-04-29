"""Built-in ``create_tool`` — SPEC-021 R-031.

Persists a new ``@tool``-decorated Python source file under
``<workspace>/.capabilities/<name>.py``, AST-validates it, and
returns the path. Does NOT auto-call ``reload`` — the LLM is
expected to call ``reload`` once after writing one or more new
capabilities.
"""

from __future__ import annotations

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool
from arcagent.tools._dynamic_loader import ASTValidationError, AstValidator

_CAPABILITIES_SUBDIR = ".capabilities"


@tool(
    name="create_tool",
    description=(
        "Author a new @tool-decorated Python file in the workspace. "
        "Validates AST before writing. Call reload() after to register."
    ),
    classification="state_modifying",
    capability_tags=["self_modification"],
    when_to_use=("When the agent decides it needs a new tool that doesn't exist yet."),
    requires_skill="create-tool",
    version="1.0.0",
)
async def create_tool(name: str, source: str) -> str:
    """Write ``source`` to ``workspace/.capabilities/<name>.py``.

    Fails if the name already exists or if AST validation rejects the
    source. Returns the path on success.
    """
    if not name.isidentifier():
        return f"Error: name {name!r} is not a valid Python identifier"
    workspace = _runtime.workspace()
    target_dir = workspace / _CAPABILITIES_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}.py"
    if target.exists():
        return (
            f"Error: tool {name!r} already exists at {target.relative_to(workspace)}; "
            f"use update_tool to change it"
        )
    try:
        AstValidator().validate(source)
    except ASTValidationError as exc:
        return f"Error: AST validation rejected source — {exc}"
    target.write_text(source, encoding="utf-8")
    return f"Created tool {name!r} at {target.relative_to(workspace)}"
