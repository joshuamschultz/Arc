"""Built-in ``reload`` tool — SPEC-021 R-030.

The single self-modification trigger. Calls
:meth:`CapabilityLoader.reload` and returns the diff string per
R-005. The LLM is expected to call this after writing a new tool or
skill file with ``write`` / ``create_tool`` / ``create_skill``.
"""

from __future__ import annotations

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool


@tool(
    name="reload",
    description=(
        "Rescan all capability roots and register newly-added or "
        "changed tools and skills. Returns a diff summary."
    ),
    classification="state_modifying",
    capability_tags=["self_modification"],
    when_to_use=(
        "After writing a new .py tool or skill folder, call reload to "
        "make it available. Call ONCE — not after every write."
    ),
    version="1.0.0",
    examples=("reload()",),
)
async def reload() -> str:
    """Rescan capability roots and return the diff string."""
    return await _runtime.loader().reload()
