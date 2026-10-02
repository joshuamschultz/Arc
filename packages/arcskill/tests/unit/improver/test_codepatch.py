"""SPEC-044 Phase 4 — code-repair mutation path (REQ-010/011/012/016).

Two levels:
* the ``ArcSkillImprover`` facade drives the *whole* code path from primitive signals
  (observe → on_turn_end → maybe_improve) through propose → golden-gate → apply →
  reload — no direct ``engine.optimize`` call (producers-unwired defense);
* the patch is committed ONLY through the injected operator-anchored
  ``SkillRevisionWriter``; with no writer, or when the writer refuses, nothing is
  applied and nothing reloads (REQ-012, W0-skill one signing authority).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome

_BUGGY = b"def add(a, b):\n    return a - b\n"
_FIXED = b"def add(a, b):\n    return a + b\n"
_GOLDEN = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
_NODE = "evals/test_calc.py::test_add"


class _FakeMutator:
    def __init__(self, patch: BundlePatch) -> None:
        self._patch = patch

    async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
        assert kind == "code"
        return self._patch


class _FakeRunner:
    """before (buggy scripts) → fail; after (fixed overlay) → pass — no real sandbox."""

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        fixed = _FIXED in view.scripts.values()
        return [EvalOutcome(case_id=c.id, passed=fixed) for c in cases]


def _make_skill(root: Path) -> Path:
    sk = root / "skill_traces_ignored" / "calc-skill"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# Calc\n", encoding="utf-8")
    (sk / "scripts" / "calc.py").write_bytes(_BUGGY)
    (sk / "evals" / "test_calc.py").write_text(_GOLDEN, encoding="utf-8")
    return sk / "SKILL.md"


@pytest.mark.asyncio
async def test_facade_drives_code_repair_end_to_end(tmp_path: Path, dir_writer: Any) -> None:
    """observe→improve commits a gated code patch and reloads — the real facade path."""
    skill_md = _make_skill(tmp_path)
    reloaded: list[bool] = []
    writer = dir_writer(lambda _name: skill_md.parent)
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier="personal",
        mutator=_FakeMutator(BundlePatch(files={"scripts/calc.py": _FIXED}, summary="fix add")),
        eval_runner=_FakeRunner(),
        writer=writer,
        skill_path=lambda name: skill_md,
        reload=lambda: reloaded.append(True),
    )

    await imp.observe(
        skill_name="calc-skill", tool_name="run", status="error", error_type="AssertionError"
    )
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()

    assert writer.commits == [("calc-skill", {"scripts/calc.py": _FIXED}, "fix add")]
    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _FIXED
    assert reloaded == [True]


@pytest.mark.asyncio
async def test_facade_rejects_patch_that_does_not_fix_suite(tmp_path: Path) -> None:
    """A patch that fails the golden gate is never applied (strict improvement)."""
    skill_md = _make_skill(tmp_path)
    reloaded: list[bool] = []
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier="personal",
        # Patch keeps the bug → runner reports fail before AND after → no improvement.
        mutator=_FakeMutator(BundlePatch(files={"scripts/calc.py": _BUGGY}, summary="noop")),
        eval_runner=_FakeRunner(),
        skill_path=lambda name: skill_md,
        reload=lambda: reloaded.append(True),
    )
    await imp.observe(
        skill_name="calc-skill", tool_name="run", status="error", error_type="AssertionError"
    )
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()

    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _BUGGY  # unchanged
    assert reloaded == []


async def _repair_with(tmp_path: Path, writer: Any) -> tuple[Path, list[bool]]:
    skill_md = _make_skill(tmp_path)
    reloaded: list[bool] = []
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier="personal",
        mutator=_FakeMutator(BundlePatch(files={"scripts/calc.py": _FIXED}, summary="fix add")),
        eval_runner=_FakeRunner(),
        writer=writer,
        skill_path=lambda name: skill_md,
        reload=lambda: reloaded.append(True),
    )
    await imp.observe(
        skill_name="calc-skill", tool_name="run", status="error", error_type="AssertionError"
    )
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()
    return skill_md, reloaded


@pytest.mark.asyncio
async def test_code_patch_without_a_writer_is_never_applied(tmp_path: Path) -> None:
    """No operator-anchored writer → the gated patch is refused, nothing reloads."""
    skill_md, reloaded = await _repair_with(tmp_path, writer=None)
    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _BUGGY
    assert reloaded == []


@pytest.mark.asyncio
async def test_code_patch_refused_by_the_writer_leaves_the_skill_unchanged(
    tmp_path: Path,
) -> None:
    """A writer refusal (stale head, bad signature) applies nothing and reloads nothing."""

    class _Refusing:
        def commit(self, skill_name: str, files: Any, *, reason: str) -> str:
            raise ValueError("active skill revision changed since review")

    skill_md, reloaded = await _repair_with(tmp_path, writer=_Refusing())
    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _BUGGY
    assert reloaded == []
