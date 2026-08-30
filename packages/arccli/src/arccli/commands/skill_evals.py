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
* ``arc skill evals promote <skill_path> <spec.json>`` — the operator-facing golden
  curation loop (H-041). Reads a curation spec (gate_type + ideal/assertions/rubric),
  and emits a SIGNED + REDACTED golden case under ``evals/curated/`` via the ONE
  ``arcskill.improver.emit_golden_case`` operation the arcui surface also wraps. A
  ``judge_rubric`` spec without a pinned judge id + rubric sha256 is rejected.
* ``arc skill evals judge <skill_path> <candidate_output>`` — score a candidate's
  produced output against the skill's ``judge_rubric`` curated cases with the REAL
  pinned judge (H-041c). The reviewing operator runs this on a candidate that the
  auto-improver routed to review (a suite with any judge case can't auto-promote,
  because the deterministic sandbox can't score it). Drives ``evaluate_curated`` — the
  one place a real judge verdict is computed, fail-closed — so a missing judge or a
  non-"pass" verdict FAILS and exits nonzero.
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
from typing import TYPE_CHECKING

from arccli.commands._shared import err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _write

if TYPE_CHECKING:
    from arcskill.improver.models import EvalCase
    from arcskill.improver.seams import LLMInvoker

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
    elif target[0] == "promote":
        if len(target) != 3:
            err("Usage: arc skill evals promote <skill_path> <spec.json>")
            sys.exit(2)
        _promote(
            Path(target[1]).expanduser().resolve(),
            Path(target[2]).expanduser().resolve(),
        )
    elif target[0] == "judge":
        if len(target) != 3:
            err("Usage: arc skill evals judge <skill_path> <candidate_output>")
            sys.exit(2)
        _judge(
            Path(target[1]).expanduser().resolve(),
            Path(target[2]).expanduser().resolve(),
            as_json=args.json,
        )
    else:
        if len(target) != 1:
            err("Usage: arc skill evals <skill_path>")
            sys.exit(2)
        _list_cases(Path(target[0]).expanduser().resolve())


def _load_cases(skill_dir: Path) -> list[EvalCase]:
    """Discover golden cases via arcskill's static AST scan."""
    try:
        from arcskill.improver.evalgate import load_suite
    except ImportError:
        err("Error: arcskill is not installed; install it to manage eval suites.")
        sys.exit(1)
    return load_suite(skill_dir)


def _list_cases(skill_dir: Path) -> None:
    """List golden cases with provenance; static AST only, exit 0."""
    cases = _load_cases(skill_dir)
    if not cases:
        _write("No eval cases found.")
        return
    rows = [[case.id, _provenance(case), case.gate_type] for case in cases]
    _print_table(["Case", "Provenance", "Gate"], rows)


def _provenance(case: EvalCase) -> str:
    """Human-readable provenance: curated wins, else machine/human."""
    if case.curated:
        return "curated"
    return "machine" if case.machine_authored else "human"


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


# ---------------------------------------------------------------------------
# promote — the operator-facing golden curation loop (H-041)
# ---------------------------------------------------------------------------


def _promote(skill_dir: Path, spec_path: Path) -> None:
    """Emit a signed + redacted golden from a curation spec — the ONE emit operation."""
    try:
        from arcskill.improver import CuratedGoldenCase, emit_golden_case
        from arcskill.improver.goldencase import CurationError
    except ImportError:
        err("Error: arcskill is not installed; install it to curate golden cases.")
        sys.exit(1)
    import json

    if not spec_path.is_file():
        err(f"Error: no such spec file: {spec_path}")
        sys.exit(1)
    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
        case = CuratedGoldenCase.model_validate(raw)
    except (ValueError, OSError) as exc:
        err(f"Error: invalid curation spec: {exc}")
        sys.exit(1)
    try:
        # Same operation the arcui "promote to golden" surface wraps. The CLI path is
        # personal-tier (no agent signer in scope); federal signing rides the agent.
        emitted = emit_golden_case(skill_dir, case)
    except CurationError as exc:
        err(f"Error: {exc}")
        sys.exit(1)
    _write(f"Emitted curated golden {emitted.nodeid} (gate_type={emitted.case.gate_type}).")


# ---------------------------------------------------------------------------
# judge — the operator-facing REAL judge verdict (H-041c)
# ---------------------------------------------------------------------------


def _make_judge(model_id: str) -> LLMInvoker:
    """Build an arcllm-backed judge for the case's PINNED model (structural LLMInvoker).

    Seam kept module-level so tests inject a deterministic judge without touching arcllm.
    """
    import arcllm

    provider, _, model_name = model_id.partition("/") if "/" in model_id else (model_id, "", None)

    class _ArcLLMJudge:
        async def invoke(self, prompt: str) -> str:
            model = arcllm.load_model(provider, model_name or None)
            try:
                messages = [arcllm.Message(role="user", content=[arcllm.TextBlock(text=prompt)])]
                resp = await model.invoke(messages)
                return resp.content or ""
            finally:
                await model.close()

    return _ArcLLMJudge()


def _judge(skill_dir: Path, output_path: Path, *, as_json: bool) -> None:
    """Score a candidate's produced output against the skill's judge_rubric cases.

    Reuses ``ArcSkillImprover.evaluate_curated`` — the one place a real judge verdict is
    computed, fail-closed — so the judge never runs anywhere else. Exits nonzero if any
    judge case fails (a non-"pass" verdict, or a missing judge).
    """
    import asyncio
    import json
    import tempfile

    try:
        from arcskill.improver import ArcSkillImprover
        from arcskill.improver.curation import load_curated_cases
    except ImportError:
        err("Error: arcskill is not installed; install it to run judge evals.")
        sys.exit(1)

    if not output_path.is_file():
        err(f"Error: no such candidate-output file: {output_path}")
        sys.exit(1)
    candidate_output = output_path.read_text(encoding="utf-8")

    judge_cases = [c for c in load_curated_cases(skill_dir) if c.gate_type == "judge_rubric"]
    if not judge_cases:
        _write("No judge_rubric cases to score.")
        return
    models = {c.judge_model_id for c in judge_cases}
    if len(models) != 1:
        err(
            f"Error: judge cases pin {len(models)} distinct judge models "
            f"({', '.join(sorted(models))}); score a suite with a single pinned judge."
        )
        sys.exit(1)
    (model_id,) = tuple(models)
    judge = _make_judge(model_id)
    by_id = {c.case_id: c for c in judge_cases}
    skill_name = judge_cases[0].skill_name

    def _resolve(_name: str) -> Path:
        return skill_dir / "SKILL.md"

    with tempfile.TemporaryDirectory(prefix="arc-judge-") as tmp:
        improver = ArcSkillImprover(Path(tmp), skill_path=_resolve)
        all_verdicts = asyncio.run(
            improver.evaluate_curated(skill_name, candidate_output, judge=judge)
        )
    verdicts = [v for v in all_verdicts if v.case_id in by_id]

    if as_json:
        _write(
            json.dumps(
                [
                    {
                        "case_id": v.case_id,
                        "passed": v.passed,
                        "judge_model_id": model_id,
                        "rubric_sha256": by_id[v.case_id].rubric_sha256,
                        "detail": v.detail,
                    }
                    for v in verdicts
                ],
                indent=2,
            )
        )
    else:
        _write(f"Judge verdicts for {skill_dir} (judge={model_id}):")
        for v in verdicts:
            rubric = by_id[v.case_id].rubric_sha256
            _write(
                f"  {'PASS' if v.passed else 'FAIL'}  {v.case_id}  "
                f"rubric={rubric[:12]}  {v.detail}"
            )
    failed = [v for v in verdicts if not v.passed]
    if failed:
        err(f"{len(failed)} of {len(verdicts)} judge case(s) failed (fail-closed).")
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
