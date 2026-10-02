"""W0-skill (h): one signing authority. The improver commits, it never signs or writes.

Every improver write (prose apply, code repair, merge, golden curation, suite
adoption) goes through the injected :class:`SkillRevisionWriter`. arcagent backs it
with the operator-anchored revision chain, so the agent's DID key never signs a
capability artifact, and history, versions and rollback show the change. With no
writer wired the improver fails closed: status ``unavailable``, nothing written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig, seams
from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.codepatch import apply_bundle_patch
from arcskill.improver.curation import emit_golden_case
from arcskill.improver.engine import SkillOptimizer
from arcskill.improver.goldencase import CuratedGoldenCase
from arcskill.improver.models import BundlePatch, BundleView, Candidate, EvalCase, EvalOutcome
from arcskill.improver.seams import SkillRevisionWriter, SkillWriterUnavailableError
from arcskill.improver.suitegen import SuiteGenerator

_SEED = "---\nname: s\n---\n# s\n\nDo the thing.\n"
_BETTER = "---\nname: s\n---\n# s\n\nDo the thing carefully.\n"


def _skill(tmp_path: Path) -> Path:
    folder = tmp_path / "skills" / "s"
    (folder / "evals").mkdir(parents=True)
    (folder / "SKILL.md").write_text(_SEED, encoding="utf-8")
    (folder / "evals" / "test_g.py").write_text(
        "def test_a():\n    assert 1\n\ndef test_b():\n    assert 1\n", encoding="utf-8"
    )
    return folder / "SKILL.md"


def test_the_signer_seam_is_gone(dir_writer: Any) -> None:
    assert not hasattr(seams, "Signer")
    assert isinstance(dir_writer(lambda _n: Path(".")), SkillRevisionWriter)


def test_apply_result_commits_through_the_writer_and_never_writes_in_place(
    tmp_path: Path,
) -> None:
    skill_md = _skill(tmp_path)
    commits: list[tuple[str, dict[str, bytes], str]] = []

    class _Writer:
        def commit(self, skill_name: str, files: Any, *, reason: str) -> str:
            commits.append((skill_name, dict(files), reason))
            return "d" * 64

    optimizer = SkillOptimizer(
        config=AsyncMock(),
        evaluator=AsyncMock(),
        reflector=AsyncMock(),
        guardrails=AsyncMock(),
        store=CandidateStore(tmp_path / "ws"),
        writer=_Writer(),
    )
    digest = optimizer.apply_result(
        "s",
        Candidate(id="c1", text=_BETTER, generation=1),
        skill_path=skill_md,
        seed_scores={"accuracy": 1.0},
        trace_ids=["t1"],
    )
    assert digest == "d" * 64
    assert commits == [("s", {"SKILL.md": _BETTER.encode()}, commits[0][2])]
    assert commits[0][2]
    assert skill_md.read_text(encoding="utf-8") == _SEED
    assert not skill_md.with_name("SKILL.md.arcsig").exists()


def test_apply_result_without_a_writer_refuses(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    optimizer = SkillOptimizer(
        config=AsyncMock(),
        evaluator=AsyncMock(),
        reflector=AsyncMock(),
        guardrails=AsyncMock(),
        store=CandidateStore(tmp_path / "ws"),
    )
    with pytest.raises(SkillWriterUnavailableError):
        optimizer.apply_result(
            "s",
            Candidate(id="c1", text=_BETTER, generation=1),
            skill_path=skill_md,
            seed_scores={},
            trace_ids=[],
        )
    assert skill_md.read_text(encoding="utf-8") == _SEED


def test_code_patch_commits_through_the_writer(tmp_path: Path, dir_writer: Any) -> None:
    skill_md = _skill(tmp_path)
    writer = dir_writer(lambda _name: tmp_path / "committed")
    patch = BundlePatch(files={"scripts/run.py": b"print('fixed')\n"}, summary="fix run")
    apply_bundle_patch("s", patch, writer=writer)
    assert writer.commits == [("s", {"scripts/run.py": b"print('fixed')\n"}, "fix run")]
    assert not (skill_md.parent / "scripts").exists()


@pytest.mark.parametrize("relpath", ["../escape.py", "/abs.py", "scripts/../../x.py", ""])
def test_code_patch_refuses_paths_outside_the_bundle(
    tmp_path: Path, relpath: str, dir_writer: Any
) -> None:
    writer = dir_writer(lambda _name: tmp_path / "committed")
    with pytest.raises(ValueError):
        apply_bundle_patch("s", BundlePatch(files={relpath: b"x"}, summary="bad"), writer=writer)
    assert writer.commits == []


def test_golden_curation_commits_case_anchor_and_manifest_in_one_revision(
    tmp_path: Path, dir_writer: Any
) -> None:
    skill_md = _skill(tmp_path)
    writer = dir_writer(lambda _name: tmp_path / "committed")
    case = CuratedGoldenCase(
        case_id="c",
        skill_name="s",
        gate_type="exact_match",
        source_trace_id="t",
        ideal_output="42",
    )
    emitted = emit_golden_case(skill_md.parent, case, writer=writer)
    assert len(writer.commits) == 1
    _name, files, _reason = writer.commits[0]
    assert set(files) == {
        emitted.case_path,
        emitted.anchor_path,
        "evals/.manifest.json",
    }
    assert emitted.revision
    assert not (skill_md.parent / "evals" / "curated").exists()


@pytest.mark.asyncio
async def test_suite_generator_returns_files_and_writes_nothing(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)

    class _LLM:
        async def invoke(self, prompt: str) -> str:
            return "def test_x():\n    assert run() == 1\n"

    class _Runner:
        async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
            poisoned = "negative-control" in view.text
            return [EvalOutcome(case_id=c.id, passed=not poisoned) for c in cases]

    from arcskill.improver.config import SuiteConfig

    generator = SuiteGenerator(llm=_LLM(), runner=_Runner(), config=SuiteConfig(flake_runs=1))
    view = BundleView("s", _SEED, skill_md.parent)
    result = await generator.generate("s", view)
    assert result.adopted
    assert set(result.files) == {"evals/test_golden_generated.py", "evals/.manifest.json"}
    assert not (skill_md.parent / "evals" / "test_golden_generated.py").exists()


class _Runner:
    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        better = "carefully" in view.text
        return [EvalOutcome(case_id=c.id, passed=c.id.endswith("test_a") or better) for c in cases]


def _improver(tmp_path: Path, skill_md: Path, writer: Any) -> ArcSkillImprover:
    class _LLM:
        async def invoke(self, prompt: str) -> str:
            return ""

    return ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(),
        llm=_LLM(),
        eval_runner=_Runner(),
        writer=writer,
        agent_did="did:arc:test:agent",
        skill_path=lambda name: skill_md if name == "s" else None,
    )


async def _seed_traces(imp: ArcSkillImprover) -> None:
    for turn in range(3):
        await imp.observe(
            skill_name="s",
            tool_name="bash",
            status="ok",
            error_type=None,
            call_id=f"c{turn}",
            run_id=f"r{turn}",
        )
        await imp.on_turn_end(turn=turn, outcome="", run_id=f"r{turn}")


def _stub_optimizer(imp: ArcSkillImprover, monkeypatch: pytest.MonkeyPatch) -> None:
    from arcskill.improver.models import OptimizeResult

    async def optimize(self: Any, skill_name: str, current: str, traces: Any, **_: Any) -> Any:
        best = Candidate(
            id="abc123def456", text=_BETTER, generation=1, aggregate_scores={"a": 2.0}
        )
        return OptimizeResult(
            skill_name=skill_name,
            best_candidate=best,
            frontier=[best],
            iterations_run=1,
            stop_reason="done",
            seed_scores={"a": 1.0},
            improvement={"a": 1.0},
        )

    monkeypatch.setattr(SkillOptimizer, "optimize", optimize)


@pytest.mark.asyncio
async def test_improve_now_without_a_writer_is_unavailable_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md, writer=None)
    await _seed_traces(imp)
    _stub_optimizer(imp, monkeypatch)
    result = await imp.improve_now(skill_name="s", dry_run=False)
    assert result["status"] == "unavailable"
    assert skill_md.read_text(encoding="utf-8") == _SEED


@pytest.mark.asyncio
async def test_improve_now_applies_through_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_writer: Any
) -> None:
    skill_md = _skill(tmp_path)
    writer = dir_writer(lambda _name: tmp_path / "committed")
    imp = _improver(tmp_path, skill_md, writer=writer)
    await _seed_traces(imp)
    _stub_optimizer(imp, monkeypatch)
    result = await imp.improve_now(skill_name="s", dry_run=False)
    assert result["status"] == "applied", result
    assert [(name, files) for name, files, _ in writer.commits] == [
        ("s", {"SKILL.md": _BETTER.encode()})
    ]
    assert skill_md.read_text(encoding="utf-8") == _SEED


def test_curate_golden_without_a_writer_raises(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md, writer=None)
    case = CuratedGoldenCase(
        case_id="c",
        skill_name="s",
        gate_type="exact_match",
        source_trace_id="t",
        ideal_output="42",
    )
    with pytest.raises(SkillWriterUnavailableError):
        imp.curate_golden(case)
