"""Architecture regression guard: arcrun must NOT import arcllm.registry or call load_model().

PLAN.md TX.1.1 — Separation of concerns: arcrun is the agentic loop runtime;
arcllm is the LLM provider layer. arcrun MUST NOT call arcllm.registry.load_model()
directly. All model lifecycle (provider selection, connection pooling, circuit breakers)
lives in arcllm, not in the execution loop.

The constraint from Arc CLAUDE.md:
    "all llm calls are arcllm"
    "loop execution is arcrun"
    "Don't have arcrun do things that belong to agent or arcllm."

arcrun CAN import from arcllm.types (e.g. Message, Tool, TextBlock) because these
are plain data types. What it MUST NOT do is import from arcllm.registry (the provider
factory) or call load_model() — that crosses the layer boundary.

If arcrun calls load_model(), it:
  1. Takes responsibility for provider selection (belongs to arcllm).
  2. Creates its own httpx connection pool, bypassing arcllm's circuit breakers.
  3. Makes arcrun tests depend on the full arcllm provider stack.
  4. Violates the "loop = loop, model = model" separation that lets arcrun be
     backend-agnostic.

Concrete test (TX.1.1):
    AST-scan every .py file under packages/arcrun/src/arcrun/.
    Fail if any file:
      - Imports from arcllm.registry (``from arcllm.registry import ...``
        or ``import arcllm.registry``).
      - Contains a bare call to ``load_model(`` anywhere.

Allowed:
    - ``from arcllm.types import ...`` (plain data types — OK)
    - ``import arcllm.types`` (same)
    - Any other arcllm submodule that is NOT ``registry``

This test runs on every CI build (TX.1.1 in PLAN.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_violations(path: Path) -> list[str]:
    """Return violation descriptions for a Python file.

    Detects:
    - ``from arcllm.registry import <anything>``
    - ``import arcllm.registry``
    - Any call ``load_model(...)`` (bare function call, any scope)

    Args:
        path: Path to a Python source file.

    Returns:
        List of human-readable violation strings. Empty if the file is clean.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []  # Syntax errors are caught by the linter; skip here.

    violations: list[str] = []

    for node in ast.walk(tree):
        # --- Import violations ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "arcllm.registry" or alias.name.startswith("arcllm.registry."):
                    violations.append(
                        f"{path}:{node.lineno}: "
                        f"import {alias.name} — arcrun must not import arcllm.registry"
                    )

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "arcllm.registry" or module.startswith("arcllm.registry."):
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    f"{path}:{node.lineno}: "
                    f"from {module} import {names} — arcrun must not import arcllm.registry"
                )

        # --- load_model() call violations ---
        elif isinstance(node, ast.Call):
            # Direct call: ``load_model(...)``
            if isinstance(node.func, ast.Name) and node.func.id == "load_model":
                violations.append(
                    f"{path}:{node.lineno}: "
                    f"call to load_model() — model lifecycle belongs in arcllm, not arcrun. "
                    f"Receive the model as a dependency instead."
                )
            # Attribute call: ``registry.load_model(...)`` or
            # ``arcllm.registry.load_model(...)``
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "load_model":
                violations.append(
                    f"{path}:{node.lineno}: "
                    f"call to .load_model() — model lifecycle belongs in arcllm, not arcrun."
                )

    return violations


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_no_arcrun_calls_load_model() -> None:
    """AST-scan arcrun; fail if any file imports arcllm.registry or calls load_model().

    Arc separation-of-concerns (CLAUDE.md):
      - arcllm owns all LLM provider calls including load_model()
      - arcrun owns the agentic loop (planning, dispatch, tool execution)
      - arcrun receives a pre-configured model object via dependency injection

    If this test fails:
      1. Remove the arcllm.registry import from the failing arcrun file.
      2. Accept the model object via the class __init__ or function parameter.
      3. The caller (arcagent or arcgateway) is responsible for constructing
         the model via arcllm.registry.load_model() and injecting it.

    Allowed imports from arcllm:
      - ``from arcllm.types import Message, Tool, TextBlock, ...``  (data types)
      - Any other non-registry arcllm submodule

    Forbidden:
      - ``from arcllm.registry import load_model`` (or any other registry member)
      - ``import arcllm.registry``
      - Any call to ``load_model(`` anywhere in arcrun source
    """
    arcrun_src = Path(__file__).parent.parent.parent / "packages" / "arcrun" / "src" / "arcrun"
    assert arcrun_src.exists(), (
        f"arcrun source not found at {arcrun_src}. Is the packages/arcrun directory present?"
    )

    all_violations: list[str] = []
    for py_file in sorted(arcrun_src.rglob("*.py")):
        violations = _find_violations(py_file)
        all_violations.extend(violations)

    if all_violations:
        msg = (
            "ARCHITECTURE VIOLATION: arcrun imports arcllm.registry or calls load_model().\n\n"
            "Separation of concerns (Arc CLAUDE.md):\n"
            "  - arcllm: all LLM provider calls, connection pooling, circuit breakers\n"
            "  - arcrun: agentic loop (plan → dispatch → tool → respond)\n"
            "  arcrun must receive a pre-configured model via dependency injection,\n"
            "  NOT construct one by calling load_model() itself.\n\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in all_violations) + "\n\n"
            "To fix:\n"
            "  1. Remove the arcllm.registry import from the violating arcrun file.\n"
            "  2. Accept the model as a constructor parameter or function argument.\n"
            "  3. The upstream caller (arcagent, arcgateway) calls load_model().\n"
            "  Importing arcllm.types (plain data types) is still allowed.\n"
        )
        raise AssertionError(msg)
