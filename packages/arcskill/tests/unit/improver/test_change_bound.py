"""SPEC-044 Phase 5 — SkillOpt change-bound gate (REQ-030/031, AC-4).

Bounds are enforced BEFORE the sandboxed eval; tier flows through construction so the
federal floor is non-relaxable and audit events are tier-stamped (§8 tier-must-flow).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.config import ChangeBoundConfig
from arcskill.improver.guardrails import TIER_BOUNDS, ChangeBound
from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome

_BASE = {"scripts/a.py": b"def f():\n    return 1\n"}


def test_tier_defaults_are_pinned() -> None:
    """The pinned SkillOpt Lt tiers: personal 8 / enterprise 4 / federal 2."""
    assert TIER_BOUNDS["personal"].max_edits == 8
    assert TIER_BOUNDS["enterprise"].max_edits == 4
    assert TIER_BOUNDS["federal"].max_edits == 2
    assert TIER_BOUNDS["federal"].max_lines_changed == 15
    assert TIER_BOUNDS["federal"].edit_schedule == "cosine"


def test_within_bound_accepts() -> None:
    patch = BundlePatch(files={"scripts/a.py": b"def f():\n    return 2\n"})
    ok, _ = ChangeBound("federal").check(patch, _BASE)
    assert ok is True


def test_too_many_files_rejected_at_federal() -> None:
    patch = BundlePatch(files={"scripts/a.py": b"x\n", "scripts/b.py": b"y\n"})
    ok, reason = ChangeBound("federal").check(patch, _BASE)  # federal max_files_touched=1
    assert ok is False
    assert "files_touched" in reason


def test_too_many_lines_rejected() -> None:
    big = ("\n".join(f"    x{i} = {i}" for i in range(50)) + "\n").encode()
    patch = BundlePatch(files={"scripts/a.py": big})
    ok, reason = ChangeBound("federal").check(patch, _BASE)  # federal max_lines_changed=15
    assert ok is False
    assert "lines_changed" in reason


def test_federal_floor_non_relaxable() -> None:
    """An override that tries to LOOSEN the federal bound is clamped to the floor."""
    override = ChangeBoundConfig(max_lines_changed=500, max_files_touched=9)
    bound = ChangeBound("federal", override)
    assert bound.limits.max_lines_changed == 15  # not 500
    assert bound.limits.max_files_touched == 1  # not 9


def test_personal_relax_is_wider_than_federal() -> None:
    assert ChangeBound("personal").limits.max_lines_changed == 80
    assert ChangeBound("federal").limits.max_lines_changed == 15


def test_per_skill_override_tightens_within_ceiling() -> None:
    """A per-skill override may tighten below the tier ceiling but never above it."""
    patch = BundlePatch(files={"scripts/a.py": b"def f():\n    return 2\n"})
    bound = ChangeBound("personal")  # ceiling max_files_touched=3
    ok, _reason = bound.check(patch, _BASE, skill_override=ChangeBoundConfig(max_files_touched=1))
    assert ok is True  # 1 file <= tightened ceiling 1
    two = BundlePatch(files={"scripts/a.py": b"x\n", "scripts/b.py": b"y\n"})
    ok2, _ = bound.check(two, _BASE, skill_override=ChangeBoundConfig(max_files_touched=1))
    assert ok2 is False  # 2 files > tightened ceiling 1


def test_cosine_schedule_decays_ceiling_to_floor() -> None:
    bound = ChangeBound("enterprise")  # max_edits=4, floor=2, cosine
    assert bound.scheduled_edits(0, 3) == 4  # first attempt = ceiling
    assert bound.scheduled_edits(2, 3) == 2  # last attempt = floor


# --- AC-4: over-bound patch rejected PRE-eval, audited, tier-stamped -------------


class _TrackingRunner:
    def __init__(self) -> None:
        self.called = False

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        self.called = True
        return [EvalOutcome(case_id=c.id, passed=True) for c in cases]


class _OverBoundMutator:
    async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
        # Touches 2 files — over the federal max_files_touched=1 ceiling.
        return BundlePatch(files={"scripts/a.py": b"a\n", "scripts/b.py": b"b\n"})


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def write(self, event: object) -> None:
        self.events.append(event)


def _skill(root: Path) -> Path:
    sk = root / "s"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# s\n", encoding="utf-8")
    (sk / "scripts" / "a.py").write_bytes(b"def f():\n    return 1\n")
    (sk / "evals" / "test_g.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    return sk / "SKILL.md"


@pytest.mark.asyncio
async def test_ac4_over_bound_rejected_pre_eval_and_audited(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    runner = _TrackingRunner()
    sink = _Sink()
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(min_traces=1, trace_buffer_turns=0, optimize_after_uses=1,
                              min_golden_cases=1),
        tier="federal",
        mutator=_OverBoundMutator(),
        eval_runner=runner,
        audit_sink=sink,
        skill_path=lambda name: skill_md,
    )
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="ValueError")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()

    assert runner.called is False  # rejected BEFORE the sandbox eval
    bound_events = [
        e for e in sink.events if getattr(e, "action", "") == "skill.mutation.bound_rejected"
    ]
    assert bound_events, "expected a bound-rejection audit event"
    assert getattr(bound_events[0], "tier", None) == "federal"  # tier flows from construction
