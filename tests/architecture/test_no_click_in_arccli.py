"""Architecture regression guard: no `import click` or `from click` in arccli.

SDD §3.11: arccli = terminal slash-command interface (not Click).
Only arcui retains Click.

Allowlisted legacy files (temporarily until T1.1.5 full handler migration):
- agent.py        — TODO(T1.1.5): migrate to plain CommandDef handler
- ext.py          — TODO(T1.1.5): migrate to plain CommandDef handler
- formatting.py   — TODO(T1.1.5): replace click.echo with print()
- init_wizard.py  — TODO(T1.1.5): migrate to plain CommandDef handler
- llm.py          — TODO(T1.1.5): migrate to plain CommandDef handler
- main_legacy.py  — permanent legacy fallback, intentionally uses Click
- module_walkthrough.py — TODO(T1.1.5): migrate
- run.py          — TODO(T1.1.5): migrate to plain CommandDef handler
- skill.py        — TODO(T1.1.5): migrate to plain CommandDef handler
- team.py         — TODO(T1.1.5): migrate to plain CommandDef handler
- telegram_setup.py — TODO(T1.1.5): migrate or decouple from Click
- ui.py           — TODO(T1.1.5): migrate to plain CommandDef handler

This test is written so that it PASSES now (with allowlisted files) and will
FAIL again if new files introduce Click imports — acting as a ratchet.
Remove allowlist entries as each file completes migration in future tasks.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Files temporarily allowlisted during migration — remove as each is migrated.
_ALLOWLISTED: frozenset[str] = frozenset(
    {
        # Legacy Click-based handler files — pending T1.1.5 migration
        "agent.py",
        "ext.py",
        "formatting.py",
        "init_wizard.py",
        "llm.py",
        "module_walkthrough.py",
        "run.py",
        "skill.py",
        "team.py",
        "telegram_setup.py",
        "ui.py",
        # Permanent legacy fallback entry point — intentionally uses Click
        "main_legacy.py",
    }
)


def _find_click_imports(path: Path) -> list[str]:
    """Return list of violation descriptions for a Python file."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []  # skip unparseable files

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "click" or alias.name.startswith("click."):
                    violations.append(
                        f"{path}:{node.lineno}: import {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module == "click" or node.module.startswith("click.")
            ):
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    f"{path}:{node.lineno}: from {node.module} import {names}"
                )
    return violations


def test_no_click_imports_in_arccli() -> None:
    """AST-scan arccli source; fail if any non-allowlisted file imports click.

    This test acts as a ratchet: it passes once legacy files are allowlisted,
    but will fail if NEW files are added that import click. Remove entries from
    _ALLOWLISTED as migration of each file completes.
    """
    arccli_src = Path(__file__).parent.parent.parent / "packages" / "arccli" / "src" / "arccli"
    assert arccli_src.exists(), f"arccli source not found at {arccli_src}"

    all_violations: list[str] = []

    for py_file in sorted(arccli_src.rglob("*.py")):
        if py_file.name in _ALLOWLISTED:
            continue
        violations = _find_click_imports(py_file)
        all_violations.extend(violations)

    if all_violations:
        msg = (
            "Found click imports in NEW (non-allowlisted) arccli files.\n"
            "Only arcui may use Click. arccli uses the slash-command registry.\n\n"
            + "\n".join(f"  {v}" for v in all_violations)
            + "\n\nTo fix: migrate the file to slash-command handlers. "
            "If migration is genuinely deferred, add the filename to _ALLOWLISTED "
            "in tests/architecture/test_no_click_in_arccli.py with a TODO comment "
            "and reason. This allowlist is a ratchet — only shrinks over time."
        )
        raise AssertionError(msg)
