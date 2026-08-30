"""H-042 — Curator consolidate/merge (SPEC-044 lifecycle gap-closure).

``SkillLifecycle`` gained ``consolidation_candidates()`` (pure detection) and
``merge()`` (the non-destructive transition, reversible via the existing ``revive``).
``ArcSkillImprover.review_consolidation`` drives the propose -> gate -> approve -> apply
pipeline through the SAME ``EvalGate`` and operator-approval ladder as any other
mutation — never a hot-swap. These tests exercise both layers with no rigged fixtures:
the improver-level tests drive the real detection + the real apply path, only the LLM
(Merger) and the sandbox (EvalRunner) are injected fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.config import LifecycleConfig
from arcskill.improver.lifecycle import STATE_ACTIVE, STATE_MERGED, SkillLifecycle
from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome

# ---------------------------------------------------------------------------
# SkillLifecycle.consolidation_candidates() / merge() — pure, no LLM, no sandbox
# ---------------------------------------------------------------------------


def _lifecycle(
    tmp_path: Path, texts: dict[str, str], *, states: dict[str, str] | None = None
) -> tuple[SkillLifecycle, CandidateStore]:
    store = CandidateStore(tmp_path)
    for name in texts:
        (tmp_path / "skill_traces" / name).mkdir(parents=True, exist_ok=True)
    for name, state in (states or {}).items():
        store.set_lifecycle_state(name, state, reason="test setup")
    lc = SkillLifecycle(
        store,
        LifecycleConfig(),
        load_traces=lambda _n: [],
        generation_of=lambda _n: 0,
        text_of=lambda n: texts.get(n),
    )
    return lc, store


_ALPHA = "# daily-brief\nCap the brief at five items. Label facts vs inferences.\n"
_ALPHA_TWIN = "# morning-brief\nCap the brief at five items. Label facts vs inferences.\n"
_BETA = "# unrelated-skill\nParse vendor invoices into GL codes for the monthly close.\n"


def test_consolidation_candidates_flags_near_duplicate_bodies(tmp_path: Path) -> None:
    lc, _ = _lifecycle(tmp_path, {"a": _ALPHA, "b": _ALPHA_TWIN})
    candidates = lc.consolidation_candidates()
    assert len(candidates) == 1
    assert {candidates[0].skill_a, candidates[0].skill_b} == {"a", "b"}
    assert candidates[0].similarity >= 0.72
    assert "similarity" in candidates[0].reason


def test_consolidation_candidates_ignores_dissimilar_bodies(tmp_path: Path) -> None:
    lc, _ = _lifecycle(tmp_path, {"a": _ALPHA, "b": _BETA})
    assert lc.consolidation_candidates() == []


def test_consolidation_candidates_excludes_retired_and_merged_skills(tmp_path: Path) -> None:
    lc, _ = _lifecycle(
        tmp_path,
        {"a": _ALPHA, "b": _ALPHA_TWIN, "c": _ALPHA_TWIN},
        states={"b": "retired", "c": "merged"},
    )
    # "a" alone is active; nothing left to pair it with.
    assert lc.consolidation_candidates() == []


def test_consolidation_candidates_without_text_of_is_inert(tmp_path: Path) -> None:
    store = CandidateStore(tmp_path)
    for name in ("a", "b"):
        (tmp_path / "skill_traces" / name).mkdir(parents=True)
    lc = SkillLifecycle(
        store, LifecycleConfig(), load_traces=lambda _n: [], generation_of=lambda _n: 0
    )
    assert lc.consolidation_candidates() == []


def test_merge_transitions_state_and_records_lineage(tmp_path: Path) -> None:
    lc, store = _lifecycle(tmp_path, {"a": _ALPHA, "b": _ALPHA_TWIN})
    event = lc.merge("b", into="a", reason="near-duplicate")
    assert event.to_state == STATE_MERGED
    assert store.lifecycle_state("b") == STATE_MERGED
    assert store.merge_target("b") == "a"
    assert store.merge_target("a") is None  # the survivor carries no merge target


def test_revive_restores_a_merged_skill(tmp_path: Path) -> None:
    lc, store = _lifecycle(tmp_path, {"a": _ALPHA, "b": _ALPHA_TWIN})
    lc.merge("b", into="a", reason="near-duplicate")
    event = lc.revive("b")
    assert event.from_state == STATE_MERGED
    assert event.to_state == STATE_ACTIVE
    assert store.lifecycle_state("b") == STATE_ACTIVE


# ---------------------------------------------------------------------------
# ArcSkillImprover.review_consolidation — the propose -> gate -> approve -> apply E2E
# ---------------------------------------------------------------------------


class _FakeMerger:
    """Deterministic Merger: always proposes the SAME merged text (no LLM)."""

    def __init__(self, merged_text: str) -> None:
        self._merged_text = merged_text
        self.calls: list[tuple[str, str]] = []

    async def propose(self, *, a: BundleView, b: BundleView, insight: str) -> BundlePatch | None:
        self.calls.append((a.skill_name, b.skill_name))
        return BundlePatch(files={"SKILL.md": self._merged_text.encode("utf-8")}, summary="merged")


class _FakeRunner:
    """Golden-suite outcomes keyed by bundle text, mirroring test_improver_gate.py."""

    def __init__(self, passing: dict[str, set[str]]) -> None:
        self._passing = passing

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        p = self._passing.get(view.text, set())
        return [EvalOutcome(case_id=c.id, passed=c.id in p) for c in cases]


class _AutoApprover:
    def __init__(self, *, grant: bool = True) -> None:
        self._grant = grant
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, action: str, skill_name: str, detail: str) -> bool:
        self.calls.append((action, skill_name))
        return self._grant


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def write(self, event: object) -> None:
        self.events.append(event)


def _make_skill(root: Path, name: str, text: str, case_name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    evals_dir = skill_dir / "evals"
    evals_dir.mkdir()
    (evals_dir / f"test_{case_name}.py").write_text(
        f"def test_{case_name}():\n    assert 1\n", encoding="utf-8"
    )


def _skill_path_fn(root: Path) -> Callable[[str], Path | None]:
    def fn(name: str) -> Path | None:
        candidate = root / name / "SKILL.md"
        return candidate if candidate.exists() else None

    return fn


_MERGED_TEXT = "# combined-brief\nMerged skill body.\n"


def _seed_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Two active, near-duplicate skills each with their own one-case golden suite."""
    ws = tmp_path / "ws"
    skills_root = tmp_path / "skills"
    _make_skill(skills_root, "skill-a", _ALPHA, "x")
    _make_skill(skills_root, "skill-b", _ALPHA_TWIN, "y")
    for name in ("skill-a", "skill-b"):
        (ws / "skill_traces" / name).mkdir(parents=True)
    return ws, skills_root


@pytest.mark.asyncio
async def test_review_consolidation_applies_merge_when_gate_passes_and_approved(
    tmp_path: Path,
) -> None:
    ws, skills_root = _seed_pair(tmp_path)
    runner = _FakeRunner(
        {
            _ALPHA: {"evals/test_x.py::test_x"},
            _ALPHA_TWIN: {"evals/test_y.py::test_y"},
            _MERGED_TEXT: {"evals/test_x.py::test_x", "evals/test_y.py::test_y"},
        }
    )
    merger = _FakeMerger(_MERGED_TEXT)
    approver = _AutoApprover()
    sink = _Sink()
    imp = ArcSkillImprover(
        ws,
        config=ImproverConfig(),
        tier="federal",
        eval_runner=runner,
        merger=merger,
        approval_provider=approver,
        audit_sink=sink,
        skill_path=_skill_path_fn(skills_root),
    )

    await imp.review_consolidation(turn=1)

    assert merger.calls == [("skill-a", "skill-b")]
    assert ("skill.lifecycle.consolidate", "skill-a") in approver.calls
    assert imp._candidate_store.lifecycle_state("skill-b") == STATE_MERGED
    assert imp._candidate_store.merge_target("skill-b") == "skill-a"
    assert imp.retired_skills() == frozenset({"skill-b"})
    # The survivor's file was actually rewritten (not a paper transition).
    assert (skills_root / "skill-a" / "SKILL.md").read_text(encoding="utf-8") == _MERGED_TEXT
    # skill-b's own file is untouched — it's the manifest state that changed.
    assert (skills_root / "skill-b" / "SKILL.md").read_text(encoding="utf-8") == _ALPHA_TWIN

    merged_events = [
        e for e in sink.events if getattr(e, "action", "") == "skill.lifecycle.merged"
    ]
    assert merged_events and getattr(merged_events[0], "tier", None) == "federal"
    applied_events = [
        e
        for e in sink.events
        if getattr(e, "action", "") == "skill.mutation.applied"
        and getattr(e, "target", "") == "skill-a"
    ]
    assert applied_events


@pytest.mark.asyncio
async def test_review_consolidation_blocked_on_regression_never_asks_approval(
    tmp_path: Path,
) -> None:
    ws, skills_root = _seed_pair(tmp_path)
    # The merged text drops skill-b's case entirely — a regression on skill-b's suite.
    runner = _FakeRunner(
        {
            _ALPHA: {"evals/test_x.py::test_x"},
            _ALPHA_TWIN: {"evals/test_y.py::test_y"},
            _MERGED_TEXT: {"evals/test_x.py::test_x"},
        }
    )
    merger = _FakeMerger(_MERGED_TEXT)
    approver = _AutoApprover()
    imp = ArcSkillImprover(
        ws,
        config=ImproverConfig(),
        tier="federal",
        eval_runner=runner,
        merger=merger,
        approval_provider=approver,
        skill_path=_skill_path_fn(skills_root),
    )

    await imp.review_consolidation(turn=1)

    assert approver.calls == []  # the gate rejects before approval is ever asked
    assert imp._candidate_store.lifecycle_state("skill-b") == STATE_ACTIVE
    assert (skills_root / "skill-a" / "SKILL.md").read_text(encoding="utf-8") == _ALPHA


@pytest.mark.asyncio
async def test_review_consolidation_fails_closed_without_approver_at_federal(
    tmp_path: Path,
) -> None:
    ws, skills_root = _seed_pair(tmp_path)
    runner = _FakeRunner(
        {
            _ALPHA: {"evals/test_x.py::test_x"},
            _ALPHA_TWIN: {"evals/test_y.py::test_y"},
            _MERGED_TEXT: {"evals/test_x.py::test_x", "evals/test_y.py::test_y"},
        }
    )
    sink = _Sink()
    imp = ArcSkillImprover(
        ws,
        config=ImproverConfig(),
        tier="federal",
        eval_runner=runner,
        merger=_FakeMerger(_MERGED_TEXT),
        audit_sink=sink,
        skill_path=_skill_path_fn(skills_root),
        # no approval_provider wired
    )

    await imp.review_consolidation(turn=1)

    assert imp._candidate_store.lifecycle_state("skill-b") == STATE_ACTIVE
    assert (skills_root / "skill-a" / "SKILL.md").read_text(encoding="utf-8") == _ALPHA
    denied = [e for e in sink.events if getattr(e, "outcome", "") == "denied_no_approver"]
    assert denied


@pytest.mark.asyncio
async def test_review_consolidation_is_a_noop_without_a_merger(tmp_path: Path) -> None:
    ws, skills_root = _seed_pair(tmp_path)
    imp = ArcSkillImprover(
        ws, config=ImproverConfig(), tier="personal", skill_path=_skill_path_fn(skills_root)
    )
    assert imp._merger is None  # no LLM seam -> no default merger
    await imp.review_consolidation(turn=1)  # must not raise
    assert imp._candidate_store.lifecycle_state("skill-b") == STATE_ACTIVE
