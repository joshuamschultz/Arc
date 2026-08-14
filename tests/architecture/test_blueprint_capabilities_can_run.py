"""A blueprint capability that imports agent internals can never run.

Blueprint capabilities land in an agent's ``capabilities/`` directory, which
:func:`arcagent.capabilities.capability_loader.root_trust` classifies UNTRUSTED —
agent-writable, therefore contained. Contained code is executed by ArcRun in an
isolated backend that provides only an inert ``@tool`` decorator shim: no
``arcagent`` package, and no workspace mount at all. So a capability that reaches
for ``arcagent.builtins``/``core``/``modules``, or that persists anything to the
workspace, fails on every single invocation.

This shipped. All four blueprints carried capabilities written as if they ran
in-process; a sales agent's ``crm_pipeline`` died with ``No module named
'arcagent.builtins'`` every time it was called, for months, on a live box. The
unit tests were green throughout because they import the file directly with
``importlib`` and call ``_runtime.configure(workspace=tmp_path)`` — reproducing
the one environment the capability never actually gets.

The fix for such a capability is not to sign it: containment there is by design,
not a missing signature. Work that needs the workspace belongs in a **skill**,
which directs the agent to use the builtin file tools — those are TRUSTED, run
in-process, and already reach the workspace.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BLUEPRINTS = _REPO_ROOT / "blueprints"

#: Importing any of these means the capability expects the agent's own process.
_AGENT_INTERNALS = ("arcagent.builtins", "arcagent.core", "arcagent.modules")

#: What the isolation shim really provides — the only arcagent import that works.
_SHIMMED = ("arcagent.tools",)


def _blueprint_capabilities() -> list[Path]:
    return sorted(_BLUEPRINTS.glob("*/capabilities/*.py"))


def _internal_imports(source: Path) -> set[str]:
    """Every agent-internal module ``source`` imports, by dotted name."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        for name in names:
            if name in _SHIMMED:
                continue
            if any(
                name == internal or name.startswith(f"{internal}.")
                for internal in _AGENT_INTERNALS
            ):
                found.add(name)
    return found


def test_blueprints_ship_capabilities_that_can_actually_run() -> None:
    """No shipped capability may depend on the process it will never run in."""
    offenders = {
        str(path.relative_to(_REPO_ROOT)): sorted(imports)
        for path in _blueprint_capabilities()
        if (imports := _internal_imports(path))
    }

    assert not offenders, (
        "these blueprint capabilities import agent internals but run CONTAINED, so "
        "every invocation fails with ModuleNotFoundError and no workspace to write "
        "to. Move the behaviour into a skill that drives the builtin file tools "
        f"(read/write/ls/grep), which do run in-process: {offenders}"
    )
