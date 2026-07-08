"""SPEC-044 Phase 3 — golden-task eval gate (REQ-020/021/022, strict-improvement)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver.evalgate import EvalGate, load_suite
from arcskill.improver.models import BundleView, EvalCase, EvalOutcome


class _FakeRunner:
    """Deterministic EvalRunner: maps (skill text) -> set of passing case ids."""

    def __init__(self, passing_by_text: dict[str, set[str]]) -> None:
        self._passing = passing_by_text

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        passing = self._passing.get(view.text, set())
        return [EvalOutcome(case_id=c.id, passed=c.id in passing) for c in cases]


def _cases(n: int) -> list[EvalCase]:
    return [EvalCase(id=f"c{i}", node=f"evals/test_x.py::test_{i}") for i in range(n)]


def _view(text: str) -> BundleView:
    return BundleView(skill_name="s", text=text)


# -- load_suite ----------------------------------------------------------------


def test_load_suite_discovers_pytest_cases(tmp_path: Path) -> None:
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "test_golden.py").write_text(
        "def test_happy():\n    assert True\n\nasync def test_async():\n    assert True\n\n"
        "def helper():\n    pass\n",
        encoding="utf-8",
    )
    cases = load_suite(tmp_path)
    nodes = {c.node for c in cases}
    assert nodes == {"evals/test_golden.py::test_happy", "evals/test_golden.py::test_async"}


def test_load_suite_empty_when_no_evals_dir(tmp_path: Path) -> None:
    assert load_suite(tmp_path) == []
    assert load_suite(None) == []


# -- strict improvement (REQ-022) ---------------------------------------------


@pytest.mark.asyncio
async def test_accepts_strict_improvement() -> None:
    cases = _cases(3)
    runner = _FakeRunner({"before": {"c0"}, "after": {"c0", "c1"}})
    gate = EvalGate(runner)
    dec = await gate.decide(
        before=_view("before"), after=_view("after"), cases=cases, tier="federal", kind="prose"
    )
    assert dec.accepted is True
    assert dec.newly_passing == 1


@pytest.mark.asyncio
async def test_rejects_regression_even_with_higher_score() -> None:
    """A candidate that fixes one case but breaks another is rejected (REQ-022)."""
    cases = _cases(3)
    runner = _FakeRunner({"before": {"c0", "c1"}, "after": {"c1", "c2"}})  # c0 regressed
    gate = EvalGate(runner)
    dec = await gate.decide(
        before=_view("before"), after=_view("after"), cases=cases, tier="federal", kind="prose"
    )
    assert dec.accepted is False
    assert "regression" in dec.reason


@pytest.mark.asyncio
async def test_rejects_tie_no_new_pass() -> None:
    cases = _cases(3)
    runner = _FakeRunner({"before": {"c0"}, "after": {"c0"}})
    gate = EvalGate(runner)
    dec = await gate.decide(
        before=_view("before"), after=_view("after"), cases=cases, tier="personal", kind="prose"
    )
    assert dec.accepted is False
    assert "no strict improvement" in dec.reason


# -- no-suite tier policy (REQ-021) -------------------------------------------


@pytest.mark.asyncio
async def test_no_suite_code_blocked_every_tier() -> None:
    gate = EvalGate(_FakeRunner({}))
    for tier in ("personal", "enterprise", "federal"):
        dec = await gate.decide(
            before=_view("b"), after=_view("a"), cases=[], tier=tier, kind="code"
        )
        assert dec.accepted is False
        assert "code mutation blocked" in dec.reason


@pytest.mark.asyncio
async def test_no_suite_prose_personal_warn_else_block() -> None:
    gate = EvalGate(_FakeRunner({}))
    personal = await gate.decide(
        before=_view("b"), after=_view("a"), cases=[], tier="personal", kind="prose"
    )
    assert personal.accepted is True
    for tier in ("enterprise", "federal"):
        dec = await gate.decide(
            before=_view("b"), after=_view("a"), cases=[], tier=tier, kind="prose"
        )
        assert dec.accepted is False


@pytest.mark.asyncio
async def test_code_mutation_requires_min_cases() -> None:
    gate = EvalGate(_FakeRunner({"before": set(), "after": {"c0"}}), min_golden_cases=3)
    dec = await gate.decide(
        before=_view("before"), after=_view("after"), cases=_cases(2), tier="federal", kind="code"
    )
    assert dec.accepted is False
    assert "requires >= 3 golden cases" in dec.reason
