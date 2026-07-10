"""Task 27/32 — no `_runtime.py` module may hold per-agent state as a
module-level global.

The embedded gateway (SPEC-023, canonical at every tier) runs many
``ArcAgent`` instances concurrently in ONE process (up to 32, per
``arcui.embedded_agents._BoundedAgentCache``), with ``SessionRouter.handle()``
spawning one ``asyncio.Task`` per session — different agents' turns
interleave on the same event loop. A ``_runtime.py`` module that binds
per-agent state (workspace, identity, config, ...) via a plain module-level
global, reassigned through ``global X; X = ...`` inside ``configure()``, is
silently overwritten by whichever agent's task most recently called
``configure()`` — corrupting every OTHER already-loaded agent's in-flight
tool calls with the wrong workspace, config, and (task 27's live incident)
the wrong signing IDENTITY.

The fix is ``contextvars.ContextVar``: each ``asyncio.Task`` gets its own
isolated value, so a sibling task's ``configure()`` call is invisible.
A ``ContextVar``-based module never needs the ``global`` keyword — mutation
happens via ``.set()`` on the (immutable, module-level) ``ContextVar``
object itself, never by rebinding the module attribute name. That makes
presence of a ``global`` statement inside a ``_runtime.py`` file a precise,
low-maintenance signal for the forbidden pattern — this test is AST-based
(mirrors ``arcgateway/tests/architecture/test_imports.py``) so it never
imports the module under test; a violation is reported as a clean test
failure, not a runtime data-corruption bug discovered in production.

Modules with a legitimate ``contextvars``-based background-task binding
(e.g. an explicit ``contextvars.copy_context()`` handoff into a spawned
task) are allow-listed by name with a comment explaining why — that is a
DIFFERENT, safe pattern from a plain ``global`` reassignment and this test
must not flag it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ARCAGENT_SRC = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent"

# Modules with no legitimate reason to ever use `global` in a _runtime.py —
# empty today; add an entry here ONLY with a comment justifying why a
# specific module's background-task lifecycle requires it, and prefer
# `contextvars.copy_context()` handoff over `global` even then.
_ALLOWED_GLOBAL_USERS: frozenset[str] = frozenset()


def _all_runtime_files() -> list[Path]:
    if not _ARCAGENT_SRC.exists():
        return []
    return sorted(_ARCAGENT_SRC.rglob("_runtime.py"))


def _global_statements(path: Path) -> list[str]:
    """Return the variable names targeted by any `global` statement in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.extend(node.names)
    return names


def test_arcagent_runtime_files_exist() -> None:
    """Sanity guard: this test must actually be exercising real files, not
    silently passing because the glob matched nothing (e.g. a moved
    package root)."""
    files = _all_runtime_files()
    assert len(files) >= 17, (
        f"expected at least 17 _runtime.py files under {_ARCAGENT_SRC}, found "
        f"{len(files)} — architecture test may be scanning the wrong root"
    )


def test_no_runtime_module_uses_global_for_agent_state() -> None:
    """No `_runtime.py` file may reassign a module-level name via `global`.

    A `global` statement inside `configure()`/`reset()` is exactly the
    signature of the task-27 anti-pattern: state visible to every agent's
    tool calls in the process, silently overwritten by the last one to
    configure it. `contextvars.ContextVar` never needs `global` — `.set()`
    mutates the ContextVar object, it never rebinds the module attribute.
    """
    violations: list[tuple[Path, list[str]]] = []
    for path in _all_runtime_files():
        rel = path.relative_to(_REPO_ROOT)
        module_name = path.parent.name
        if module_name in _ALLOWED_GLOBAL_USERS:
            continue
        globals_used = _global_statements(path)
        if globals_used:
            violations.append((rel, globals_used))

    assert not violations, (
        "_runtime.py files using `global` to mutate module-level agent state "
        "(convert to contextvars.ContextVar — see "
        "arcagent/builtins/capabilities/_runtime.py for the target pattern):\n"
        + "\n".join(f"  {p}: global {', '.join(names)}" for p, names in violations)
    )
