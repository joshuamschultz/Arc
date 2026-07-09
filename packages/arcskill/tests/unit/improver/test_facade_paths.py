"""SPEC-044 — ArcSkillImprover facade branch coverage (prose path + helpers).

Exercises the facade branches the mechanism tests don't reach directly: the prose
optimize path, per-skill frontmatter override parsing, exempt-tag skipping, and the
code-eligibility predicate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.config import ChangeBoundConfig
from arcskill.improver.models import SkillTrace


class _EmptyLLM:
    """LLMInvoker that returns nothing — evaluator scores 1, reflector proposes nothing."""

    async def invoke(self, prompt: str) -> str:
        return ""


def _cfg() -> ImproverConfig:
    return ImproverConfig(min_traces=1, trace_buffer_turns=0, optimize_after_uses=1)


@pytest.mark.asyncio
async def test_prose_path_seed_result_does_not_apply(tmp_path: Path) -> None:
    """A prose skill with no improving candidate leaves the seed in place (no reload)."""
    sk = tmp_path / "s"
    sk.mkdir()
    skill_md = sk / "SKILL.md"
    skill_md.write_text("## Steps\n1. do a thing\n", encoding="utf-8")
    reloaded: list[bool] = []
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        llm=_EmptyLLM(),
        skill_path=lambda name: skill_md,
        reload=lambda: reloaded.append(True),
    )
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="E")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()
    assert reloaded == []  # seed candidate → nothing applied


def test_skill_override_read_from_frontmatter(tmp_path: Path) -> None:
    sk = tmp_path / "s"
    sk.mkdir()
    skill_md = sk / "SKILL.md"
    skill_md.write_text(
        "---\nname: s\nimprover:\n  max_lines_changed: 5\n  max_files_touched: 1\n---\n# body\n",
        encoding="utf-8",
    )
    imp = ArcSkillImprover(
        tmp_path / "ws", config=_cfg(), tier="personal", skill_path=lambda name: skill_md
    )
    override = imp._skill_override("s")
    assert isinstance(override, ChangeBoundConfig)
    assert override.max_lines_changed == 5


def test_skill_override_none_without_frontmatter(tmp_path: Path) -> None:
    sk = tmp_path / "s"
    sk.mkdir()
    skill_md = sk / "SKILL.md"
    skill_md.write_text("# no frontmatter\n", encoding="utf-8")
    imp = ArcSkillImprover(
        tmp_path / "ws", config=_cfg(), tier="personal", skill_path=lambda name: skill_md
    )
    assert imp._skill_override("s") is None


@pytest.mark.asyncio
async def test_exempt_skill_is_not_optimized(tmp_path: Path) -> None:
    """A security-critical (exempt-tagged) skill is skipped by eligibility."""
    sk = tmp_path / "s"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    skill_md = sk / "SKILL.md"
    skill_md.write_text("---\nname: s\ntags: [security-critical]\n---\n# body\n", encoding="utf-8")
    (sk / "scripts" / "a.py").write_bytes(b"x = 1\n")
    (sk / "evals" / "test_g.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")

    class _Mut:
        called = False

        async def propose(self, **kw: object) -> None:
            _Mut.called = True
            return None

    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        mutator=_Mut(),
        skill_path=lambda name: skill_md,
    )
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="E")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()
    assert _Mut.called is False  # exempt → never proposed


def test_should_repair_code_false_without_scripts(tmp_path: Path) -> None:
    sk = tmp_path / "s"
    sk.mkdir()
    skill_md = sk / "SKILL.md"
    skill_md.write_text("# prose only\n", encoding="utf-8")

    class _Mut:
        async def propose(self, **kw: object) -> None:
            return None

    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        mutator=_Mut(),
        skill_path=lambda name: skill_md,
    )
    trace = SkillTrace(
        trace_id="t",
        session_id="s",
        skill_name="s",
        skill_version=0,
        turn_number=0,
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    assert imp._should_repair_code(skill_md, [trace]) is False
