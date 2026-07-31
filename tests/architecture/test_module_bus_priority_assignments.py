"""Architecture test: module bus subscription priorities for key M1/M2 modules.

PLAN.md TX.1.5 — Verify priority assignments for modules where ordering matters.

Priority semantics (module_bus.py docstring):
    Lower values run first: 10=policy/security, 50=security, 100=default, 200=logging.
    Same-priority handlers run CONCURRENTLY via asyncio.gather.

Key facts this test enforces:
  1. The vault module's MODULE.yaml declares priority ≤ 10 (resolves before others).
  2. skill_improver subscribes at the canonical priorities from PLAN.md / SDD §3.7:
       agent:post_tool   → priority=200
       agent:post_plan   → priority=200
       agent:post_respond → priority=150
  3. memory module subscribes agent:pre_tool at priority=10 (highest — ACL guard).
  4. No two *different* modules collide at priority ≤ 50 for the same event,
     UNLESS the collision is listed in _KNOWN_PRIORITY_COLLISIONS below.

Note on default-priority collisions:
    Many modules legitimately share priority=100 for lifecycle events
    (agent:shutdown, agent:ready, etc.). These handlers run concurrently,
    which is the correct behaviour — they are independent side-effect handlers
    with no ordering requirement. This test does NOT flag those collisions.

    Only sub-50 priorities (veto zone) are checked for cross-module collisions,
    because at those levels the sequential ordering of handlers matters for
    correctness (e.g. memory ACL veto must run before memory read completes).

Known pre-existing collisions (do not introduce new ones):
    bio_memory + memory both subscribe to agent:pre_tool at priority=10.
    This pre-dates SPEC-018 and is documented in _KNOWN_PRIORITY_COLLISIONS.
    Resolving it requires assigning bio_memory a different priority for
    agent:pre_tool — tracked as a post-M1 cleanup item.

This test runs on every CI build (TX.1.5 in PLAN.md).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import yaml  # PyYAML — available in dev environment

# ---------------------------------------------------------------------------
# Canonical priority expectations from PLAN.md / SDD §3
# These are the priorities the spec REQUIRES. If code diverges, the test fails.
# ---------------------------------------------------------------------------

_CANONICAL_PRIORITIES: list[tuple[str, str, int]] = [
    ("skill_improver", "agent:post_tool", 200),
    ("skill_improver", "agent:post_plan", 200),
    ("skill_improver", "agent:post_respond", 150),
    ("memory", "agent:pre_tool", 10),
]

# Priority threshold below which cross-module collisions are errors.
# At priority >= 50, concurrent execution is expected and acceptable.
_VETO_ZONE_MAX_PRIORITY = 50

# Known pre-existing priority collisions in the veto zone.
# These were present before SPEC-018 and are documented here so the test
# does not block CI while the cleanup is tracked separately.
# Format: frozenset of module names for a given (event, priority) pair.
# DO NOT add new items here — fix the priority assignment instead.
_KNOWN_PRIORITY_COLLISIONS: dict[tuple[str, int], frozenset[str]] = {
    # bio_memory and memory both subscribe to agent:pre_tool at priority=10.
    # Needs a post-M1 cleanup: assign bio_memory a distinct priority (e.g. 15)
    # so the memory ACL guard always runs before bio_memory pre-tool hooks.
    ("agent:pre_tool", 10): frozenset({"bio_memory", "memory"}),
}


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def _load_module_yaml(path: Path) -> dict[str, Any]:
    """Load a MODULE.yaml file, returning empty dict on parse error."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# AST scanner for bus.subscribe() calls
# ---------------------------------------------------------------------------


class _SubscribeCallVisitor(ast.NodeVisitor):
    """Collect bus.subscribe(event, handler, priority=N) calls."""

    def __init__(self) -> None:
        # list of (event: str, priority: int, lineno: int)
        self.subscriptions: list[tuple[str, int, int]] = []

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "subscribe":
            event: str | None = None
            priority: int = 100  # default

            if node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str):
                    event = val

            for kw in node.keywords:
                if kw.arg == "priority" and isinstance(kw.value, ast.Constant):
                    val = kw.value.value
                    if isinstance(val, int):
                        priority = val

            if event is not None:
                self.subscriptions.append((event, priority, node.lineno))

        self.generic_visit(node)


def _scan_py_file(path: Path) -> list[tuple[str, int, int]]:
    """Return (event, priority, lineno) tuples from subscribe() calls."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    visitor = _SubscribeCallVisitor()
    visitor.visit(tree)
    return visitor.subscriptions


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------


def test_module_bus_priority_assignments() -> None:
    """Verify module bus priority assignments for key M1/M2 modules.

    Checks:
    1. No NEW two-module collisions at priority < 50 (veto zone).
       Known pre-existing collisions are documented in _KNOWN_PRIORITY_COLLISIONS.
    2. skill_improver uses canonical priorities from PLAN.md.
    3. memory module subscribes agent:pre_tool at priority=10.
    4. vault MODULE.yaml declares priority ≤ 10.
    """
    modules_root = (
        Path(__file__).parent.parent.parent
        / "packages"
        / "arcagent"
        / "src"
        / "arcagent"
        / "modules"
    )
    assert modules_root.exists(), (
        f"arcagent modules directory not found at {modules_root}."
    )

    # ------------------------------------------------------------------
    # Collect per-event subscribe priorities from source code.
    # ------------------------------------------------------------------
    # (event, priority) → [(module_name, filepath, lineno), ...]
    priority_registry: dict[tuple[str, int], list[tuple[str, str, int]]] = {}

    # module_name → [(event, priority, lineno), ...]
    module_subscriptions: dict[str, list[tuple[str, int, int]]] = {}

    for module_dir in sorted(modules_root.iterdir()):
        if not module_dir.is_dir():
            continue
        module_name = module_dir.name

        for py_file in sorted(module_dir.rglob("*.py")):
            subs = _scan_py_file(py_file)
            if not subs:
                continue

            if module_name not in module_subscriptions:
                module_subscriptions[module_name] = []
            module_subscriptions[module_name].extend(subs)

            for event, priority, lineno in subs:
                key = (event, priority)
                if key not in priority_registry:
                    priority_registry[key] = []
                priority_registry[key].append((module_name, str(py_file), lineno))

    # ------------------------------------------------------------------
    # Check for cross-module priority collisions IN THE VETO ZONE (< 50).
    # Skip known pre-existing collisions from _KNOWN_PRIORITY_COLLISIONS.
    # ------------------------------------------------------------------
    collisions: list[str] = []
    for (event, priority), registrations in sorted(priority_registry.items()):
        if priority >= _VETO_ZONE_MAX_PRIORITY:
            # Default zone (priority >= 50): concurrent execution is expected.
            continue

        modules_at_this_priority = frozenset(r[0] for r in registrations)
        if len(modules_at_this_priority) <= 1:
            continue  # Only one module — no collision.

        # Check if this collision is the known pre-existing one.
        known = _KNOWN_PRIORITY_COLLISIONS.get((event, priority))
        if known is not None and modules_at_this_priority == known:
            # Known pre-existing collision — skip, but don't suppress new additions.
            # If *more* modules are added to this collision, we want to catch it.
            continue

        detail_lines = [
            f"    {module_name} @ {filepath}:{lineno}"
            for module_name, filepath, lineno in registrations
            if module_name not in (known or frozenset())
        ]
        collisions.append(
            f"  Event '{event}' priority={priority} (VETO ZONE < {_VETO_ZONE_MAX_PRIORITY}) — "
            f"modules: {sorted(modules_at_this_priority)}\n"
            + "\n".join(detail_lines)
        )

    if collisions:
        msg = (
            "NEW MODULE BUS PRIORITY COLLISION IN VETO ZONE:\n"
            f"Two or more different modules share the same (event, priority) "
            f"where priority < {_VETO_ZONE_MAX_PRIORITY}.\n"
            f"In the veto zone, handlers run sequentially; collisions cause\n"
            f"non-deterministic ACL enforcement ordering.\n\n"
            "NEW collisions (not in _KNOWN_PRIORITY_COLLISIONS allowlist):\n"
            + "\n".join(collisions)
            + "\n\n"
            "Fix: assign distinct priorities to the colliding modules.\n"
            "Do NOT add the collision to _KNOWN_PRIORITY_COLLISIONS — fix it.\n"
            "See PLAN.md canonical priority table for guidance.\n"
        )
        raise AssertionError(msg)

    # ------------------------------------------------------------------
    # Canonical priority assertions from PLAN.md.
    # Only checked if the module actually has subscribe() calls.
    # ------------------------------------------------------------------
    canonical_failures: list[str] = []
    for expected_module, expected_event, expected_priority in _CANONICAL_PRIORITIES:
        subs = module_subscriptions.get(expected_module, [])
        if not subs:
            continue  # Module not yet implemented — skip.
        matches = [s for s in subs if s[0] == expected_event]
        if not matches:
            continue  # Module doesn't subscribe to this event yet — skip.
        priorities_used = {s[1] for s in matches}
        if expected_priority not in priorities_used:
            canonical_failures.append(
                f"  {expected_module}.subscribe('{expected_event}', ...) "
                f"expected priority={expected_priority}, "
                f"found priorities={sorted(priorities_used)}"
            )

    if canonical_failures:
        msg = (
            "CANONICAL PRIORITY MISMATCH:\n"
            "Module subscribe() calls deviate from the priorities documented in PLAN.md.\n\n"
            + "\n".join(canonical_failures)
            + "\n\n"
            "Check PLAN.md canonical priority table for expected values.\n"
        )
        raise AssertionError(msg)

    # ------------------------------------------------------------------
    # Vault MODULE.yaml priority ≤ 10.
    # ------------------------------------------------------------------
    vault_yaml_path = modules_root / "vault" / "MODULE.yaml"
    if vault_yaml_path.exists():
        vault_data = _load_module_yaml(vault_yaml_path)
        vault_priority = vault_data.get("priority")
        if vault_priority is not None:
            assert isinstance(vault_priority, int), (
                f"vault/MODULE.yaml 'priority' must be an integer, "
                f"got {type(vault_priority).__name__}: {vault_priority!r}"
            )
            assert vault_priority <= 10, (
                f"vault/MODULE.yaml priority={vault_priority} exceeds 10.\n"
                f"The vault module resolves secrets before other modules;\n"
                f"it must run first (lower priority number = earlier).\n"
                f"Expected: priority ≤ 10.  Current: priority={vault_priority}\n"
            )
