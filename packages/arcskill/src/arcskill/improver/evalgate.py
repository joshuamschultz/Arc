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
"""

from __future__ import annotations

import ast
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
    """Discover pytest golden cases under ``<skill_dir>/evals/`` (static AST scan)."""
    if skill_dir is None:
        return []
    evals_dir = skill_dir / "evals"
    if not evals_dir.is_dir():
        return []
    cases: list[EvalCase] = []
    for path in sorted(evals_dir.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        rel = path.relative_to(skill_dir).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                nodeid = f"{rel}::{node.name}"
                cases.append(EvalCase(id=nodeid, node=nodeid))
    return cases


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
        if kind == "code" and len(cases) < self._min_cases:
            return GateDecision(
                accepted=False,
                reason=f"code mutation requires >= {self._min_cases} golden cases "
                f"(have {len(cases)})",
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
