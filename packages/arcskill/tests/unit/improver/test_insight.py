"""SPEC-044 Phase 8 — optional arcmemory insight enrichment (REQ-060, D-11).

The improver works fully memory-less (insight=""); when an arcmemory ``Brain`` is present,
its ``retrieve`` output is passed as a *primitive string* through ``maybe_improve`` and
reaches the code Mutator. arcskill imports no arcmemory (enforced by the architecture test).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome


class _CapturingMutator:
    def __init__(self) -> None:
        self.seen_insight: str | None = None

    async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
        self.seen_insight = insight
        return BundlePatch(files={"scripts/a.py": b"def f():\n    return 2\n"})


class _Runner:
    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        fixed = b"return 2" in b"".join(view.scripts.values())
        return [EvalOutcome(case_id=c.id, passed=fixed) for c in cases]


def _skill(root: Path) -> Path:
    sk = root / "s"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# s\n", encoding="utf-8")
    (sk / "scripts" / "a.py").write_bytes(b"def f():\n    return 1\n")
    (sk / "evals" / "test_g.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    return sk / "SKILL.md"


async def _drive(tmp_path: Path, mutator: _CapturingMutator, insight: str) -> None:
    skill_md = _skill(tmp_path)
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier="personal",
        mutator=mutator,
        eval_runner=_Runner(),
        skill_path=lambda name: skill_md,
    )
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="ValueError")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve(insight=insight)
    await imp.aclose()


@pytest.mark.asyncio
async def test_insight_reaches_the_mutator(tmp_path: Path) -> None:
    mutator = _CapturingMutator()
    await _drive(tmp_path, mutator, insight="recurring failure: off-by-one in the loop bound")
    assert mutator.seen_insight == "recurring failure: off-by-one in the loop bound"


@pytest.mark.asyncio
async def test_memory_less_path_works(tmp_path: Path) -> None:
    """Empty insight (NullBrain / no memory) still drives the mutator fully."""
    mutator = _CapturingMutator()
    await _drive(tmp_path, mutator, insight="")
    assert mutator.seen_insight == ""


@pytest.mark.asyncio
async def test_llm_code_mutator_puts_insight_in_prompt() -> None:
    from unittest.mock import AsyncMock

    from arcskill.improver.mutate import LLMCodeMutator

    llm = AsyncMock()
    llm.invoke.return_value = "{}"
    view = BundleView("s", "# s\n", None, scripts={"scripts/a.py": b"x = 1\n"})
    await LLMCodeMutator(llm).propose(
        kind="code", current=view, failures="err", insight="INSIGHT-MARKER-123"
    )
    prompt = llm.invoke.call_args.args[0]
    assert "INSIGHT-MARKER-123" in prompt
