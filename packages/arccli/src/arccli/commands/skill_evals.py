"""`arc skill evals` — golden eval-suite management (SPEC-054, REQ-111/119).

Dispatched from ``arc skill``:

* ``arc skill evals <skill_path>`` — list discovered golden cases with provenance
  via ``arcskill.improver.evalgate.load_suite`` (static AST walk; never executes
  eval code).
* ``arc skill evals edit <skill_path> <file> [--force]`` — open a temp copy in
  ``$VISUAL``/``$EDITOR``, validate the save, warn on suite-floor breach or
  passing-anchor loss (REQ-119), and commit atomically. The manifest hash then
  no longer matches the committed bytes, so the file classifies human-authored
  (REQ-111) — removing the manifest entry would flip it back to machine.
* ``arc skill evals regen <skill_path> [--yes]`` — unified-diff preview of the
  machine-authored files; actual regeneration needs agent context (LLM invoker
  + sandbox runner), so a confirmed regen errors clearly.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from arccli.commands._shared import err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _write

if TYPE_CHECKING:
    from arcskill.improver.models import (  # type: ignore[import-untyped]  # reason: arcskill ships no py.typed marker
        EvalCase,
    )

_MIN_GOLDEN_CASES = 3


def evals_handler(args: argparse.Namespace) -> None:
    """Route `arc skill evals` invocations: list, edit, or regen."""
    target: list[str] = args.target
    if target[0] == "edit":
        if len(target) != 3:
            err("Usage: arc skill evals edit <skill_path> <file> [--force]")
            sys.exit(2)
        _edit(Path(target[1]).expanduser().resolve(), target[2], force=args.force)
    elif target[0] == "regen":
        if len(target) != 2:
            err("Usage: arc skill evals regen <skill_path> [--yes]")
            sys.exit(2)
        _regen(Path(target[1]).expanduser().resolve(), yes=args.yes)
    else:
        if len(target) != 1:
            err("Usage: arc skill evals <skill_path>")
            sys.exit(2)
        _list_cases(Path(target[0]).expanduser().resolve())


def _load_cases(skill_dir: Path) -> list[EvalCase]:
    """Discover golden cases via arcskill's static AST scan."""
    try:
        from arcskill.improver.evalgate import (  # type: ignore[import-untyped]  # reason: arcskill ships no py.typed marker
            load_suite,
        )
    except ImportError:
        err("Error: arcskill is not installed; install it to manage eval suites.")
        sys.exit(1)
    return cast("list[EvalCase]", load_suite(skill_dir))


def _list_cases(skill_dir: Path) -> None:
    """List golden cases with provenance; static AST only, exit 0."""
    cases = _load_cases(skill_dir)
    if not cases:
        _write("No eval cases found.")
        return
    rows = [[case.id, "machine" if case.machine_authored else "human"] for case in cases]
    _print_table(["Case", "Provenance"], rows)


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


def _edit(skill_dir: Path, filename: str, *, force: bool) -> None:
    """Edit one eval file through $VISUAL/$EDITOR with validate-on-save."""
    evals_dir = skill_dir / "evals"
    target = evals_dir / filename
    if not target.is_file():
        err(f"Error: no such eval file: {target}")
        sys.exit(1)
    if (evals_dir / ".improver.lock").exists():
        err("Warning: an improvement pass is in flight for this skill; it may overwrite edits.")
    original = target.read_bytes()
    edited = _run_editor(original)
    _validate_syntax(edited, filename)
    warnings = _edit_warnings(skill_dir, target, original, edited)
    for warning in warnings:
        err(warning)
    if warnings and not force:
        err("Edit rejected; re-run with --force to commit anyway.")
        sys.exit(1)
    _commit(target, edited)
    _write(f"Committed {filename}.")


def _resolve_editor() -> list[str]:
    """$VISUAL wins over $EDITOR (POSIX convention)."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        err("Error: set $VISUAL or $EDITOR to edit eval files.")
        sys.exit(1)
    return shlex.split(editor)


def _run_editor(original: bytes) -> bytes:
    """Hand the editor a temp copy; abort git-style on a nonzero editor exit."""
    fd, tmp_name = tempfile.mkstemp(prefix="arc-evals-", suffix=".py")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(original)
        proc = subprocess.run(  # noqa: S603  # reason: $VISUAL/$EDITOR is the user's own editor
            [*_resolve_editor(), str(tmp)], check=False
        )
        if proc.returncode != 0:
            err(f"Error: editor exited with status {proc.returncode}; aborting, no changes.")
            sys.exit(1)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _validate_syntax(edited: bytes, filename: str) -> None:
    """Reject a save that no longer parses; the original stays untouched."""
    try:
        ast.parse(edited.decode("utf-8"), filename=filename)
    except (UnicodeDecodeError, SyntaxError) as exc:
        err(f"Error: syntax error in edited file ({exc}); original left untouched.")
        sys.exit(1)


def _case_count(source: bytes) -> int:
    """Count test functions via AST; an unparsable module counts zero."""
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return 0
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test")
    )


def _edit_warnings(skill_dir: Path, target: Path, original: bytes, edited: bytes) -> list[str]:
    """REQ-119 pre-commit warnings: suite-floor breach (code kind) and anchor loss."""
    before = _case_count(original)
    after = _case_count(edited)
    warnings: list[str] = []
    if _is_code_kind(skill_dir):
        suite_after = _suite_count_after(skill_dir / "evals", target, after)
        if suite_after < _MIN_GOLDEN_CASES:
            warnings.append(
                f"Warning: suite drops to {suite_after} case(s), below min_golden_cases "
                f"({_MIN_GOLDEN_CASES}) for a code-kind skill."
            )
    if after < before:
        warnings.append(
            f"Warning: passing-anchor loss — {target.name} drops from {before} to {after} case(s)."
        )
    return warnings


def _is_code_kind(skill_dir: Path) -> bool:
    """A skill is code-kind when it ships executable scripts."""
    scripts = skill_dir / "scripts"
    return scripts.is_dir() and any(scripts.glob("*.py"))


def _suite_count_after(evals_dir: Path, target: Path, target_after: int) -> int:
    """Total suite case count with *target* replaced by its edited count."""
    total = target_after
    for path in sorted(evals_dir.rglob("test_*.py")):
        if path != target:
            total += _case_count(path.read_bytes())
    return total


def _commit(target: Path, edited: bytes) -> None:
    """Atomic write: temp file beside the target + os.replace, no residue."""
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".arc-commit-")
    with os.fdopen(fd, "wb") as handle:
        handle.write(edited)
    os.replace(tmp_name, target)


# ---------------------------------------------------------------------------
# regen
# ---------------------------------------------------------------------------


def _regen(skill_dir: Path, *, yes: bool) -> None:
    """Preview what regen would overwrite; the bare CLI cannot regenerate."""
    cases = _load_cases(skill_dir)
    machine_files = sorted({case.id.split("::", 1)[0] for case in cases if case.machine_authored})
    if not machine_files:
        err("Error: no machine-authored eval files to regenerate.")
        sys.exit(1)
    for rel in machine_files:
        _print_regen_diff(skill_dir, rel)
    if not yes and not _confirm("Regenerate the files above? [y/N] "):
        err("Regen aborted.")
        sys.exit(1)
    err(
        "Error: regeneration requires agent context (LLM invoker + sandbox runner); "
        "run the improver inside an agent instead."
    )
    sys.exit(1)


def _print_regen_diff(skill_dir: Path, rel: str) -> None:
    """Unified diff from the current file to the (not-yet-known) regenerated one."""
    old_lines = (skill_dir / rel).read_text(encoding="utf-8").splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, [], fromfile=f"a/{rel}", tofile=f"b/{rel}")
    sys.stdout.writelines(diff)


def _confirm(prompt: str) -> bool:
    """Prompt on stdin; EOF declines."""
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False
