"""SPEC-044 MED-5 — SkillOpt features are WIRED into the code-repair path.

(a) Cosine edit-schedule: ``_optimize_code`` passes a real, decaying ``edit_budget`` from
    ``ChangeBound.scheduled_edits`` into the bound check (not the raw ceiling).
(b) Rejected-edit buffer: a rejected patch is buffered and fed back to the mutator as
    negative feedback on the next attempt (the SkillOpt convergence lever).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome


class _AllFailRunner:
    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        return [EvalOutcome(case_id=c.id, passed=False) for c in cases]


class _Sink:
    def write(self, event: object) -> None: ...


def _seed(root: Path, *, files: int = 1) -> Path:
    sk = root / "s"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# s\n", encoding="utf-8")
    for i in range(files):
        (sk / "scripts" / f"f{i}.py").write_bytes(b"x = 1\n")
    (sk / "evals" / "test_g.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    return sk / "SKILL.md"


def _cfg() -> ImproverConfig:
    return ImproverConfig(min_traces=1, trace_buffer_turns=0, optimize_after_uses=1,
                          min_golden_cases=1)


async def _drive(imp: ArcSkillImprover) -> None:
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="E")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()


@pytest.mark.asyncio
async def test_cosine_edit_budget_threaded_into_change_bound(tmp_path: Path) -> None:
    """At a late generation the decayed floor budget is what reaches ChangeBound.check."""
    skill_md = _seed(tmp_path)

    class _Mut:
        async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
            return BundlePatch(files={"scripts/f0.py": b"x = 2\n"}, summary="tweak")

    imp = ArcSkillImprover(
        tmp_path / "ws", config=_cfg(), tier="enterprise",  # ceiling 4, floor 2, cosine/3
        mutator=_Mut(), eval_runner=_AllFailRunner(), approval_provider=None,
        skill_path=lambda name: skill_md,
    )
    captured: dict[str, int | None] = {}
    real_check = imp._change_bound.check

    def _spy(patch: object, base: object, *, skill_override: object = None,
             edit_budget: int | None = None) -> tuple[bool, str]:
        captured["edit_budget"] = edit_budget
        return real_check(patch, base, skill_override=skill_override, edit_budget=edit_budget)

    imp._change_bound.check = _spy  # type: ignore[method-assign]
    imp._guardrails.set_generation("s", 2)  # last of 3 improve attempts → decays to floor

    await _drive(imp)

    assert captured["edit_budget"] == 2  # ChangeBound.scheduled_edits(2, 3) == floor


@pytest.mark.asyncio
async def test_rejected_patch_feeds_next_mutation(tmp_path: Path) -> None:
    """A bound-rejected patch is buffered and surfaced to the mutator's next proposal."""
    skill_md = _seed(tmp_path, files=1)

    class _OverBoundMut:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
            self.seen.append(failures)
            # 4 files touched > personal ceiling (3) → bound-rejected, buffered.
            return BundlePatch(
                files={f"scripts/f{i}.py": b"y = 1\n" for i in range(4)}, summary="too big"
            )

    mut = _OverBoundMut()
    imp = ArcSkillImprover(
        tmp_path / "ws", config=_cfg(), tier="personal",
        mutator=mut, eval_runner=_AllFailRunner(), skill_path=lambda name: skill_md,
    )

    await _drive(imp)  # round 1 → over-bound → rejected + buffered
    await _drive(imp)  # round 2 → mutator should see the rejection

    assert len(mut.seen) == 2
    assert "PREVIOUSLY-REJECTED EDITS" in mut.seen[1]
    assert "too big" in mut.seen[1]
    assert "PREVIOUSLY-REJECTED EDITS" not in mut.seen[0]  # clean on the first attempt


def test_rejected_buffer_is_bounded(tmp_path: Path) -> None:
    """The rejected-edit buffer never grows unbounded (last N kept)."""
    imp = ArcSkillImprover(tmp_path / "ws", config=_cfg(), tier="personal")
    for i in range(10):
        imp._record_rejection("s", BundlePatch(files={"scripts/a.py": b"x\n"}, summary=f"p{i}"),
                              "rejected")
    assert len(imp._rejected["s"]) == 5  # _REJECTED_BUFFER_MAX
    assert "p9" in imp._rejected["s"][-1]  # newest retained
