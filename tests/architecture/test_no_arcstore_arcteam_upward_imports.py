"""Architecture regression guard: the dependency direction, one way only.

::

    arcteam                 orchestration — composes agents into a fleet
       |
       v
    arcagent, arcui, arccli, arcrun, arcgateway
       |
       v
    arcstore                foundation — knows nothing above it

**ArcTeam sits above ArcAgent, not beside it.** It adds orchestration to agents
that already run perfectly well alone: fleet membership, mail, tasks, shared
knowledge, and running one workflow across many agents — in time, across many
KINDS of agent, not only Arc's. So arcteam imports arcagent, and arcagent must
never import arcteam. An agent that reached upward could not be installed,
started or reasoned about without the layer that orchestrates it, and the
standalone promise would be a fiction.

**ArcStore stays underneath everything.** It is written to by the layers above
and knows none of them.

Concrete test: AST-scan every ``.py`` under each package's source tree and fail
on an import that points the wrong way.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Nothing under arcstore may import any of these.
_ABOVE_ARCSTORE = ("arcagent", "arcui", "arccli", "arcrun", "arcgateway", "arcteam")

#: arcteam composes agents, so arcagent is DOWNWARD for it and allowed. The
#: surfaces are not: they drive arcteam, never the reverse.
_ABOVE_ARCTEAM = ("arcui", "arccli", "arcgateway")

#: The direction that keeps an agent standalone.
_ABOVE_ARCAGENT = ("arcteam", "arcui", "arccli", "arcgateway")

#: Modules still reaching upward, being converted to the ``arcagent.fleet`` seam
#: one at a time. This list may only ever SHRINK: a module absent from it is
#: converted and must stay that way, and nothing new may be added.
#:
#: * ``messaging`` — inter-agent mail; the last and largest of them.
_UNCONVERTED_MODULES = frozenset({"messaging"})


def _find_upward_imports(path: Path, upward: tuple[str, ...]) -> list[str]:
    """Return violation descriptions for a Python file importing a higher layer.

    Args:
        path: Path to a Python source file.

    Returns:
        List of violation strings (empty if no violations found).
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []  # Skip unparseable files — syntax errors caught elsewhere

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in upward:
                    violations.append(f"{path}:{node.lineno}: import {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in upward:
                names = ", ".join(a.name for a in node.names)
                violations.append(f"{path}:{node.lineno}: from {module} import {names}")

    return violations


def _scan_package(package: str, upward: tuple[str, ...]) -> list[str]:
    """AST-scan a package's source tree for upward-layer imports.

    Args:
        package: Package directory name under packages/ (e.g. "arcstore").

    Returns:
        List of violation strings across all files in the package.
    """
    src = Path(__file__).parent.parent.parent / "packages" / package / "src" / package
    assert src.exists(), f"{package} source not found at {src}."

    violations: list[str] = []
    for py_file in sorted(src.rglob("*.py")):
        violations.extend(_find_upward_imports(py_file, upward))
    return violations


def test_no_arcstore_imports_upward() -> None:
    """arcstore is the foundation. Everything is above it; it knows none of them."""
    violations = _scan_package("arcstore", _ABOVE_ARCSTORE)

    assert not violations, (
        "ARCHITECTURE VIOLATION: arcstore imports a layer above it.\n\n"
        "Every other package depends on arcstore — never the other way around, "
        "or the dependency graph has a cycle in it.\n\n"
        "Violations found:\n" + "\n".join(f"  {v}" for v in violations) + "\n\n"
        "To fix:\n"
        "  1. Remove the upward import from arcstore.\n"
        "  2. Move the shared code down into arcstore itself.\n"
    )


def test_no_arcteam_imports_a_surface() -> None:
    """arcteam orchestrates agents; the surfaces drive arcteam, not the reverse.

    Importing arcagent is fine and expected — that is what orchestrating agents
    means. Importing arcui, arccli or arcgateway is not: those drive arcteam.
    """
    violations = _scan_package("arcteam", _ABOVE_ARCTEAM)

    assert not violations, (
        "ARCHITECTURE VIOLATION: arcteam imports a surface that drives it.\n\n"
        "arcui/arccli/arcgateway call into arcteam — never the other way "
        "around.\n\n"
        "Violations found:\n" + "\n".join(f"  {v}" for v in violations) + "\n"
    )


def _unconverted(violation: str) -> bool:
    return any(f"/modules/{name}/" in violation for name in _UNCONVERTED_MODULES)


def test_arcagent_never_imports_the_layer_that_orchestrates_it() -> None:
    """An agent must run with no orchestration layer installed at all.

    ArcTeam composes agents into a fleet and adds mail, tasks, shared knowledge
    and cross-agent workflow runs. An agent reaches those only through a seam it
    owns (``arcagent.fleet``), which the orchestration layer fills in when it
    composes the agent. Reaching upward instead would make arcteam a hidden
    requirement of every agent, and the standalone promise a fiction.
    """
    violations = [
        violation
        for violation in _scan_package("arcagent", _ABOVE_ARCAGENT)
        if not _unconverted(violation)
    ]

    assert not violations, (
        "ARCHITECTURE VIOLATION: arcagent imports the layer above it.\n\n"
        "An agent runs alone. Anything it needs from a fleet is declared as a "
        "contract in arcagent.fleet and supplied by whoever composes it.\n\n"
        "Violations found:\n" + "\n".join(f"  {v}" for v in violations) + "\n\n"
        "To fix:\n"
        "  1. Declare what the agent needs as a Protocol in arcagent.fleet.\n"
        "  2. Implement it in arcteam and pass it in as ``fleet=``.\n"
    )


def test_the_conversion_list_only_shrinks() -> None:
    """Nothing may be added to the unconverted list to make a new import pass.

    The list is an inventory of work in progress, not a permission slip. A
    module that no longer reaches upward is removed from it here, and a module
    that never did must never appear.
    """
    still_reaching = {
        name
        for name in _UNCONVERTED_MODULES
        for violation in _scan_package("arcagent", _ABOVE_ARCAGENT)
        if f"/modules/{name}/" in violation
    }

    assert still_reaching == _UNCONVERTED_MODULES, (
        "The unconverted list no longer matches reality.\n\n"
        f"Listed but already clean (remove them): "
        f"{sorted(_UNCONVERTED_MODULES - still_reaching)}\n"
        "Every module in the list must still have an upward import; when one is "
        "converted, delete it from the list so it can never regress."
    )
