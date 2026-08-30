"""Golden-task eval gate — the HARD acceptance gate (SPEC-044 REQ-020/021/022).

The LLM judge only *ranks* the frontier; a deterministic golden-task suite *decides*
acceptance. A candidate is accepted only when it makes **≥1 previously-failing** golden
case pass **and regresses none** — the strict-improvement rule (SkillOpt), which rejects
ties and neutral drift. Untrusted execution is the injected :class:`EvalRunner`'s job
(the sandbox boundary); this module owns the load + the pass/fail decision only.

Tier no-suite policy (REQ-021, fail-closed):
* **code** mutation with no suite → blocked at **every** tier (never unsafe self-mod).
* **prose** mutation with no suite → personal allows (audit-warn); enterprise/federal block.
* code mutation also requires **≥ ``min_golden_cases``** human-authorable cases (OQ-3).

Provenance (REQ-109/110/111): a case is machine-authored when its file's module
docstring carries the ``@generated`` token — cross-checked against the harness-written
manifest at ``evals/.manifest.json``, so the model can never self-assert human
authorship. A hash mismatch means a human edited the file, which reclassifies it as
human-authored. Enterprise/federal count only human-authored cases toward
``min_golden_cases``; machine cases still run and count in strict improvement.
Placeholder scaffold functions (docstring and/or ``assert True`` only) are dropped so
a placeholder-only suite classifies as empty and ``no_suite_policy`` governs.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from arcskill.improver.models import BundleView, EvalCase
from arcskill.improver.seams import EvalRunner

_logger = logging.getLogger("arcskill.improver.evalgate")


@dataclass(frozen=True)
class GateDecision:
    """Outcome of the eval gate: accept/reject + a machine-readable reason."""

    accepted: bool
    reason: str
    before_pass: int = 0
    after_pass: int = 0
    newly_passing: int = 0


def load_suite(skill_dir: Path | None) -> list[EvalCase]:
    """Discover pytest golden cases under ``<skill_dir>/evals/`` (static AST scan).

    Curated cases (H-041) carry ``provenance: "curated"`` + ``gate_type`` in the
    manifest entry for their anchor file: they are human-authored (count toward the
    gate minimum at every tier) and WIN over a machine case that collides on the same
    node id.
    """
    if skill_dir is None:
        return []
    evals_dir = skill_dir / "evals"
    if not evals_dir.is_dir():
        return []
    entries = _load_manifest_entries(evals_dir)
    manifest = {name: str(e["sha256"]) for name, e in entries.items() if "sha256" in e}
    by_nodeid: dict[str, EvalCase] = {}
    for path in sorted(evals_dir.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        machine = _is_machine_authored(path, tree, evals_dir, manifest)
        entry = entries.get(path.relative_to(evals_dir).as_posix(), {})
        curated = entry.get("provenance") == "curated"
        gate_type = str(entry.get("gate_type", "exact_match"))
        rel = path.relative_to(skill_dir).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name.startswith("test")
                and not _is_placeholder(node)
            ):
                nodeid = f"{rel}::{node.name}"
                case = EvalCase(
                    id=nodeid,
                    node=nodeid,
                    machine_authored=machine and not curated,
                    gate_type=gate_type,
                    curated=curated,
                )
                existing = by_nodeid.get(nodeid)
                # Curated wins on conflict (locked design §5); otherwise first-seen holds.
                if existing is None or (curated and not existing.curated):
                    by_nodeid[nodeid] = case
    return list(by_nodeid.values())


def _load_manifest_entries(evals_dir: Path) -> dict[str, dict[str, object]]:
    """Read the harness manifest: evals-relative filename → its full metadata entry."""
    try:
        raw = json.loads((evals_dir / ".manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(name): entry for name, entry in files.items() if isinstance(entry, dict)}


def _is_machine_authored(
    path: Path, tree: ast.Module, evals_dir: Path, manifest: dict[str, str]
) -> bool:
    """Classify one eval file's provenance (REQ-109/111).

    The ``@generated`` docstring marker alone is model-forgeable, so it never grants
    human status: marker without a manifest entry stays machine-authored. A manifest
    hash mismatch means a human edited the file since the harness wrote it, which
    reclassifies the cases as human-authored.
    """
    docstring = ast.get_docstring(tree) or ""
    if "@generated" not in docstring:
        return False
    recorded = manifest.get(path.relative_to(evals_dir).as_posix())
    if recorded is None:
        return True
    return hashlib.sha256(path.read_bytes()).hexdigest() == recorded


def _is_placeholder(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the body is only docstring(s) and/or ``assert True`` — scaffold, not a case."""
    for stmt in func.body:
        is_docstring = (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
        is_assert_true = (
            isinstance(stmt, ast.Assert)
            and isinstance(stmt.test, ast.Constant)
            and stmt.test.value is True
        )
        if not (is_docstring or is_assert_true):
            return False
    return True


class EvalGate:
    """Deterministic golden-task acceptance gate over an injected sandboxed runner."""

    def __init__(self, runner: EvalRunner, *, min_golden_cases: int = 3) -> None:
        self._runner = runner
        self._min_cases = min_golden_cases

    async def decide(
        self,
        *,
        before: BundleView,
        after: BundleView,
        cases: list[EvalCase],
        tier: str,
        kind: str,
    ) -> GateDecision:
        """Return the accept/reject decision for a candidate ``after`` vs ``before``."""
        if not cases:
            return self._no_suite_decision(tier, kind)
        if kind == "code" and _countable_cases(cases, tier) < self._min_cases:
            return GateDecision(
                accepted=False,
                reason=f"code mutation requires >= {self._min_cases} golden cases "
                f"(have {_countable_cases(cases, tier)} countable at tier={tier})",
            )
        before_pass = {o.case_id for o in await self._runner.run(before, cases) if o.passed}
        after_outcomes = await self._runner.run(after, cases)
        after_pass = {o.case_id for o in after_outcomes if o.passed}
        return self._strict_improvement(before_pass, after_pass, len(cases))

    def _strict_improvement(
        self, before_pass: set[str], after_pass: set[str], total: int
    ) -> GateDecision:
        """Accept iff ≥1 previously-failing case now passes AND none regressed."""
        regressed = before_pass - after_pass
        newly = after_pass - before_pass
        if regressed:
            return GateDecision(
                accepted=False,
                reason=f"regression on {len(regressed)} golden case(s)",
                before_pass=len(before_pass),
                after_pass=len(after_pass),
            )
        if not newly:
            return GateDecision(
                accepted=False,
                reason="no strict improvement (no previously-failing case now passes)",
                before_pass=len(before_pass),
                after_pass=len(after_pass),
            )
        return GateDecision(
            accepted=True,
            reason="strict improvement: fixed failing case(s), no regression",
            before_pass=len(before_pass),
            after_pass=len(after_pass),
            newly_passing=len(newly),
        )

    def _no_suite_decision(self, tier: str, kind: str) -> GateDecision:
        return no_suite_policy(tier, kind)


def _countable_cases(cases: list[EvalCase], tier: str) -> int:
    """Cases counting toward ``min_golden_cases`` (REQ-110).

    Machine-authored cases are supplemental at enterprise/federal — only human-authored
    cases satisfy the minimum there. Personal counts all cases.
    """
    if tier in ("enterprise", "federal"):
        return sum(1 for c in cases if not c.machine_authored)
    return len(cases)


def no_suite_policy(tier: str, kind: str) -> GateDecision:
    """Fail-closed no-suite policy (REQ-021) — runner-free, usable without a sandbox."""
    if kind == "code":
        return GateDecision(
            accepted=False, reason="code mutation blocked: no golden-task suite (any tier)"
        )
    if tier == "personal":
        _logger.warning("AUDIT WARN: prose mutation with no golden suite (personal tier)")
        return GateDecision(accepted=True, reason="prose mutation, no suite (personal audit-warn)")
    return GateDecision(
        accepted=False, reason=f"prose mutation blocked: no golden-task suite (tier={tier})"
    )


__all__ = ["EvalGate", "GateDecision", "load_suite", "no_suite_policy"]
