"""SPEC-082 T-1077 (RED + guard) — the ``mcp_server`` module: present when installed, absent-safe.

REQ-419 / COMP-001. The MCP door is an *optional* module, discovered by folder presence
and activated only by an enabled ``[modules.mcp_server]`` entry (the same rules every
module follows — ``core/module_discovery.py``). Two properties must hold at once:

1. **BEHAVIORAL (red today).** When the ``mcp_server`` module folder is present under
   ``arcagent/modules/`` and enabled in config, discovery finds it and activation yields
   it — the surface the loader and every listing agree on. This FAILS today because the
   folder does not exist yet; it goes GREEN when T-1078 lands
   ``arcagent/modules/mcp_server/`` with its ``capabilities.py`` + ``_runtime.py``. The
   door's actual serving (``server/discover`` + ``tools/list``) is proven behaviorally by
   the Phase-2 tests (T-1081, T-1083, T-1087); here we assert only the discovery/activation
   surface, which is what makes the module installable and removable.

2. **REMOVABILITY GUARD (passes today, must never break).** No file under
   ``arcagent/core/`` imports ``mcp_server``. Folder-presence discovery means the core
   nucleus must have zero knowledge of the module; a core import would make the module
   un-removable and re-couple the nucleus to an optional feature.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

from arcagent.core.config import ModuleEntry
from arcagent.core.module_discovery import active_modules, discover_modules

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARCAGENT_SRC = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent"
_MODULES_DIR = _ARCAGENT_SRC / "modules"
_CORE_DIR = _ARCAGENT_SRC / "core"

_MODULE = "mcp_server"


class _Cfg:
    """Minimal stand-in exposing the ``modules`` mapping ``active_modules`` reads."""

    def __init__(self, modules: Mapping[str, ModuleEntry]) -> None:
        self.modules = dict(modules)


def test_mcp_server_module_is_discovered_and_activates_when_enabled() -> None:
    """A present + enabled ``mcp_server`` folder is discovered and active.

    Fails today (the folder is absent, so discovery returns an empty set for the name);
    passes once T-1078 creates ``arcagent/modules/mcp_server/`` with both required files.
    """
    assert _MODULES_DIR.is_dir(), f"arcagent modules dir not found at {_MODULES_DIR}"

    discovered = discover_modules(_MODULES_DIR)
    assert _MODULE in discovered, (
        f"the {_MODULE!r} module folder is not present under {_MODULES_DIR}. "
        "T-1078 must add it with capabilities.py + _runtime.py so it is discoverable."
    )

    cfg = _Cfg({_MODULE: ModuleEntry(enabled=True)})
    assert _MODULE in active_modules(cfg, _MODULES_DIR), (
        f"{_MODULE!r} is present but does not activate when [modules.{_MODULE}] is enabled."
    )


def _imports_mcp_server(path: Path) -> list[str]:
    """Return violation strings for a file that imports the ``mcp_server`` module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _MODULE in alias.name.split("."):
                    violations.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _MODULE in module.split("."):
                names = ", ".join(a.name for a in node.names)
                violations.append(f"{path}:{node.lineno}: from {module} import {names}")
    return violations


def test_no_core_file_imports_the_mcp_server_module() -> None:
    """The nucleus must not know the door exists — folder-presence discovery only.

    Guard (invariant): passes today and must keep passing. A core import of
    ``mcp_server`` would break removability (COMP-001, REQ-419).
    """
    assert _CORE_DIR.is_dir(), f"arcagent core dir not found at {_CORE_DIR}"

    violations: list[str] = []
    for py_file in sorted(_CORE_DIR.rglob("*.py")):
        violations.extend(_imports_mcp_server(py_file))

    assert not violations, (
        "arcagent/core/ must have ZERO knowledge of the optional mcp_server module "
        "(folder-presence discovery, [modules.mcp_server] activation). Violations:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
