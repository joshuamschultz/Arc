"""SPEC-044 Phase 3 — the eval gate is WIRED into ArcSkillImprover's optimize path.

Drives ``ArcSkillImprover._gate`` (the real acceptance decision the optimize loop calls
before ``apply_result``), proving the golden-task gate governs acceptance — not a
detached, unwired module (producers-unwired defense).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.models import BundleView, EvalCase, EvalOutcome


class _FakeRunner:
    def __init__(self, passing: dict[str, set[str]]) -> None:
        self._passing = passing

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        p = self._passing.get(view.text, set())
        return [EvalOutcome(case_id=c.id, passed=c.id in p) for c in cases]


def _skill(tmp_path: Path, *, with_evals: bool) -> Path:
    sk = tmp_path / "my-skill"
    (sk).mkdir()
    (sk / "SKILL.md").write_text("# seed\n", encoding="utf-8")
    if with_evals:
        (sk / "evals").mkdir()
        (sk / "evals" / "test_g.py").write_text(
            "def test_a():\n    assert 1\n\n"
            "def test_b():\n    assert 1\n\n"
            "def test_c():\n    assert 1\n",
            encoding="utf-8",
        )
    return sk / "SKILL.md"


@pytest.mark.asyncio
async def test_no_suite_blocks_prose_at_federal(tmp_path: Path) -> None:
    path = _skill(tmp_path, with_evals=False)
    imp = ArcSkillImprover(tmp_path, config=ImproverConfig(), tier="federal")
    dec = await imp._gate("my-skill", path, "before", "after")
    assert dec.accepted is False


@pytest.mark.asyncio
async def test_no_suite_allows_prose_at_personal(tmp_path: Path) -> None:
    path = _skill(tmp_path, with_evals=False)
    imp = ArcSkillImprover(tmp_path, config=ImproverConfig(), tier="personal")
    dec = await imp._gate("my-skill", path, "before", "after")
    assert dec.accepted is True


@pytest.mark.asyncio
async def test_has_suite_default_runner_is_wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No injected runner → the concrete HubEvalRunner is the default (P3.3 wiring).

    Forced onto the personal host-fallback, the trivially-passing suite passes both
    before and after, so the strict-improvement gate rejects (no previously-failing
    case flips) — proving the real runner ran, not an unwired fail-closed stub.
    """
    from arcskill.improver.sandbox_runner import HubEvalRunner

    monkeypatch.setattr("arcskill.improver.sandbox_runner.is_firecracker_available", lambda: False)
    monkeypatch.setattr("arcskill.improver.sandbox_runner.docker_available", lambda: False)
    path = _skill(tmp_path, with_evals=True)
    imp = ArcSkillImprover(tmp_path, config=ImproverConfig(), tier="personal", eval_runner=None)
    assert isinstance(imp._eval_runner, HubEvalRunner)
    dec = await imp._gate("my-skill", path, "before", "after")
    assert dec.accepted is False
    assert "no strict improvement" in dec.reason


@pytest.mark.asyncio
async def test_has_suite_strict_improvement_accepts(tmp_path: Path) -> None:
    path = _skill(tmp_path, with_evals=True)
    runner = _FakeRunner(
        {
            "before": {"evals/test_g.py::test_a"},
            "after": {"evals/test_g.py::test_a", "evals/test_g.py::test_b"},
        }
    )
    imp = ArcSkillImprover(tmp_path, config=ImproverConfig(), tier="federal", eval_runner=runner)
    dec = await imp._gate("my-skill", path, "before", "after")
    assert dec.accepted is True
    assert dec.newly_passing == 1


@pytest.mark.asyncio
async def test_has_suite_regression_blocks(tmp_path: Path) -> None:
    path = _skill(tmp_path, with_evals=True)
    runner = _FakeRunner(
        {
            "before": {"evals/test_g.py::test_a", "evals/test_g.py::test_b"},
            "after": {"evals/test_g.py::test_b"},  # test_a regressed
        }
    )
    imp = ArcSkillImprover(tmp_path, config=ImproverConfig(), tier="federal", eval_runner=runner)
    dec = await imp._gate("my-skill", path, "before", "after")
    assert dec.accepted is False
    assert "regression" in dec.reason
