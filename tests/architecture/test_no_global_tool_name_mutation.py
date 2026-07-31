"""Architecture regression guard: arcrun must NOT mutate process-global tool-name state.

Hermes PR #4926 / PLAN.md TX.1.4 — Per-RunState ToolRegistry (no globals).

The Bug (Hermes race):
    A global variable (e.g. ``_last_resolved_tool_names``) that accumulates
    tool names across concurrent sessions becomes a shared-mutable data
    structure. Under concurrent SessionRouter dispatches, two agent tasks
    running in the same event loop can interleave writes, so one session's
    tool list bleeds into another. The symptom was non-deterministic tool
    availability per session and test flakes that only appeared at concurrency
    levels above ~5 agents.

The Fix (PLAN T3.5.7):
    Per-``RunState`` ToolRegistry. Each concurrent session gets its own
    ToolRegistry instance. No global accumulator. The AST scan below
    enforces this at the module level.

Concrete test (SDD §5, TX.1.4):
    AST-scan every .py file under packages/arcrun/src/arcrun/.
    Fail if any file:
      1. Assigns to a module-level variable whose name matches the
         global-accumulator pattern (e.g. ``_last_resolved_tool_names``,
         ``_global_tool_*``, ``_tool_names``, ``_shared_tool_*``).
      2. Augments (+=, |=) a module-level list/dict/set whose name matches
         the same pattern at module scope (not inside a class or function).

Why not just ban all module-level names?
    Module-level *constants* (e.g. ``_DEFAULT_TOOLS``) are fine — they are
    never mutated after definition. The scan distinguishes assignment targets
    that look like mutable accumulators from frozen constants by checking
    for augmented assignment (``+=``, ``|=``, ``.append(``, ``.update(``)
    and by flagging names that include the specific words that appeared in
    the Hermes bug (``last_resolved``, ``global_tool``, ``tool_names``,
    ``shared_tool``).

This test runs on every CI build (TX.1.4 in PLAN.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns that indicate a global-mutable tool-name accumulator.
# These are the exact names from Hermes' production bug plus generalizations.
# ---------------------------------------------------------------------------
_BAD_NAME_FRAGMENTS: tuple[str, ...] = (
    "_last_resolved_tool",
    "_global_tool",
    "_tool_names",
    "_shared_tool",
    "_all_tool_names",
    "_registered_tool_names",
    "_tool_name_cache",
)

# Attribute calls on module-level names that indicate mutation
_MUTATING_CALLS: tuple[str, ...] = (
    "append",
    "extend",
    "update",
    "add",
    "clear",
    "pop",
    "popitem",
    "setdefault",
    "insert",
    "remove",
    "discard",
)


def _name_is_suspicious(name: str) -> bool:
    """Return True if a variable name matches the global-accumulator pattern.

    Case-insensitive match against each bad fragment.

    Args:
        name: Identifier name (lowercased before comparison).

    Returns:
        True if the name suggests a global mutable tool-name accumulator.
    """
    lower = name.lower()
    return any(fragment in lower for fragment in _BAD_NAME_FRAGMENTS)


class _GlobalMutationVisitor(ast.NodeVisitor):
    """AST visitor that detects global-scope mutations of tool-name variables.

    Tracks the nesting depth (function/class body) so we only flag
    statements at module level (depth == 0). Inside a function or class,
    a local variable with the same name is fine.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[str] = []
        self._depth = 0  # 0 = module scope; >0 = inside function/class

    # --- Scope tracking ---

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1


    # this camelCase alias is required by the visitor protocol, not our choice.
    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    # --- Mutation detection at module scope ---

    def visit_Assign(self, node: ast.Assign) -> None:
        """Flag module-level assignments to suspicious variable names."""
        if self._depth != 0:
            self.generic_visit(node)
            return

        for target in node.targets:
            if isinstance(target, ast.Name) and _name_is_suspicious(target.id):
                self.violations.append(
                    f"{self.filename}:{node.lineno}: "
                    f"module-level assignment to suspicious name '{target.id}'. "
                    f"Per-RunState ToolRegistry required — no global tool-name state."
                )
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """Flag module-level augmented assignments (+=, |=) to suspicious names."""
        if self._depth != 0:
            self.generic_visit(node)
            return

        if isinstance(node.target, ast.Name) and _name_is_suspicious(node.target.id):
            op_name = type(node.op).__name__
            self.violations.append(
                f"{self.filename}:{node.lineno}: "
                f"module-level augmented assignment ({op_name}=) to '{node.target.id}'. "
                f"Per-RunState ToolRegistry required — no global tool-name state."
            )
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        """Flag module-level mutating method calls on suspicious names.

        Catches patterns like ``_tool_names.append(x)`` at module scope.
        """
        if self._depth != 0:
            self.generic_visit(node)
            return

        if not isinstance(node.value, ast.Call):
            self.generic_visit(node)
            return

        call = node.value
        if not isinstance(call.func, ast.Attribute):
            self.generic_visit(node)
            return

        # call.func.value should be a Name whose id is suspicious
        if (
            isinstance(call.func.value, ast.Name)
            and _name_is_suspicious(call.func.value.id)
            and call.func.attr in _MUTATING_CALLS
        ):
            self.violations.append(
                f"{self.filename}:{node.lineno}: "
                f"module-level mutating call '{call.func.value.id}.{call.func.attr}()'. "
                f"Per-RunState ToolRegistry required — no global tool-name state."
            )

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Flag module-level annotated assignments (e.g. ``_tool_names: list = []``)."""
        if self._depth != 0:
            self.generic_visit(node)
            return

        if isinstance(node.target, ast.Name) and _name_is_suspicious(node.target.id):
            self.violations.append(
                f"{self.filename}:{node.lineno}: "
                f"module-level annotated assignment to suspicious name '{node.target.id}'. "
                f"Per-RunState ToolRegistry required — no global tool-name state."
            )
        self.generic_visit(node)


def _scan_file(path: Path) -> list[str]:
    """Return violation descriptions for a single Python file.

    Args:
        path: Path to a Python source file.

    Returns:
        List of violation strings (empty if the file is clean).
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []  # Skip — syntax errors are caught by the linter, not us.

    visitor = _GlobalMutationVisitor(filename=str(path))
    visitor.visit(tree)
    return visitor.violations


def test_no_global_tool_name_mutation_in_arcrun() -> None:
    """AST-scan arcrun source; fail if any file mutates a global tool-name accumulator.

    Enforces the per-RunState ToolRegistry invariant from PLAN.md T3.5.7.

    Each concurrent session must carry its own ToolRegistry. No global list,
    dict, or set should accumulate tool names across sessions.

    If this test fails, identify the module-level variable and either:
    1. Move the accumulator into the ToolRegistry instance (preferred).
    2. Make it a frozen constant (e.g. a tuple literal, not a mutable list).
    3. If intentional shared state, document WHY in SDD §5 and add an
       explicit exception here with a comment.
    """
    arcrun_src = (
        Path(__file__).parent.parent.parent
        / "packages"
        / "arcrun"
        / "src"
        / "arcrun"
    )
    assert arcrun_src.exists(), (
        f"arcrun source not found at {arcrun_src}. "
        "Is the packages/arcrun directory present?"
    )

    all_violations: list[str] = []
    for py_file in sorted(arcrun_src.rglob("*.py")):
        violations = _scan_file(py_file)
        all_violations.extend(violations)

    if all_violations:
        msg = (
            "ARCHITECTURE VIOLATION: arcrun contains module-level mutable tool-name state.\n\n"
            "Background (Hermes PR #4926):\n"
            "  A global tool-name accumulator shared across concurrent sessions causes\n"
            "  non-deterministic tool availability and race conditions. The fix is a\n"
            "  per-RunState ToolRegistry — each session gets its own instance.\n\n"
            "Violations:\n"
            + "\n".join(f"  {v}" for v in all_violations)
            + "\n\n"
            "To fix:\n"
            "  1. Move the accumulator into the ToolRegistry class instance.\n"
            "  2. If it must be module-level, make it a frozen constant (tuple/frozenset).\n"
            "  3. Document intentional shared state with a comment + SDD §5 update.\n"
        )
        raise AssertionError(msg)
