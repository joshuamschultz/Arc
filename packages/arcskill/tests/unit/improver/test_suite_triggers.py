"""SPEC-054 T-725 (RED) — suite-generation trigger wiring + per-skill single-flight.

Pins the COMP-004 surface on ``ArcSkillImprover`` (REQ-101/106/107/108):

- ``ArcSkillImprover(..., suite_generator=<obj>)`` — keyword-only seam, default ``None``.
  The generator satisfies ``async generate(*, skill_name: str, skill_dir: Path, kind: str)
  -> None`` with ``kind`` in {"create", "extend"}.
- Lazy trigger: ``_optimize`` on a suite-less skill generates (kind="create") BEFORE the
  gate decision; ``suite.autogen = False`` disables it.
- Post-mutation extension: an applied prose candidate schedules an add-only extension
  (kind="extend"); adopted anchors stay byte-identical.
- Sweep backstop: ``await improver.sweep_suites()`` generates for suite-less skills
  most-used-first, early-exits with zero calls when every skill has a suite, and never
  double-claims a skill whose generation is already in flight.
- Single-flight: generation and optimization for one skill serialize (asserted via a
  shared max-concurrency gauge with Event-forced interleaving — an instant fake would
  pass even without the lock); two different skills interleave freely.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.config import SuiteConfig
from arcskill.improver.models import BundleView, EvalCase, EvalOutcome

SEED_TEXT = """\
## SKILL INTENT [IMMUTABLE]
Test skill intent.

## Steps
1. Do the thing carefully
2. Check the output
"""

IMPROVED_BODY = """\
## SKILL INTENT [IMMUTABLE]
Test skill intent.

## Steps
1. Do the thing carefully with IMPROVED-STEP validation
2. Check the output
"""

_ANCHORS = (
    "def test_a():\n    assert 1\n\ndef test_b():\n    assert 1\n\ndef test_c():\n    assert 1\n"
)


def _make_skill(root: Path, name: str, *, with_suite: bool, marker: str = "") -> Path:
    """A prose skill dir with an evals/ folder; anchors only when ``with_suite``."""
    sk = root / name
    (sk / "evals").mkdir(parents=True)
    body = SEED_TEXT if not marker else SEED_TEXT.replace("Do the thing", f"Do {marker}")
    (sk / "SKILL.md").write_text(body, encoding="utf-8")
    if with_suite:
        (sk / "evals" / "test_g.py").write_text(_ANCHORS, encoding="utf-8")
    return sk / "SKILL.md"


def _write_generated_suite(skill_dir: Path) -> None:
    (skill_dir / "evals").mkdir(exist_ok=True)
    (skill_dir / "evals" / "test_golden_generated.py").write_text(_ANCHORS, encoding="utf-8")


def _extend_suite(skill_dir: Path) -> None:
    """Add-only: a NEW file; existing anchor files are never rewritten."""
    (skill_dir / "evals").mkdir(exist_ok=True)
    (skill_dir / "evals" / "test_golden_extended.py").write_text(
        "def test_new():\n    assert 1\n", encoding="utf-8"
    )


class _Gauge:
    """Max-concurrency tracker shared across critical sections (overlap detector)."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        self.active -= 1


class _FakeSuiteGenerator:
    """Records calls; optionally Event-held so in-flight generation is observable.

    The Event hold is what forces real interleaving: while generation is parked on
    the event, a lockless improver WILL run a second body concurrently (the gauge
    sees max_active > 1). An instant mock could never detect the missing lock.
    """

    def __init__(
        self,
        *,
        hold: asyncio.Event | None = None,
        gauge: _Gauge | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.entered = 0
        self.exited = 0
        self._hold = hold
        self._gauge = gauge
        self._events = events

    async def generate(self, *, skill_name: str, skill_dir: Path, kind: str) -> None:
        self.entered += 1
        self.calls.append((skill_name, kind))
        if self._events is not None:
            self._events.append("generate")
        if self._gauge is not None:
            self._gauge.enter()
        try:
            if self._hold is not None:
                await self._hold.wait()
            else:
                await asyncio.sleep(0)
            if kind == "extend":
                _extend_suite(skill_dir)
            else:
                _write_generated_suite(skill_dir)
        finally:
            if self._gauge is not None:
                self._gauge.leave()
            self.exited += 1


class _ImprovingLLM:
    """Scripted LLM: judge scores the improved text 5 and the seed 2; the reflector
    proposes IMPROVED_BODY — verified to drive a full prose apply through the engine."""

    async def invoke(self, prompt: str) -> str:
        if "improving a skill procedure document" in prompt:
            return f"```markdown\n{IMPROVED_BODY}```"
        score = 5 if "IMPROVED-STEP" in prompt else 2
        return f'{{"score": {score}, "rationale": "r"}}'


class _GaugedLLM:
    """Seed-only LLM whose invoke body is a gauged critical section with real
    suspension points, so an unserialized optimization can be caught mid-flight."""

    def __init__(self, gauge: _Gauge) -> None:
        self._gauge = gauge
        self.exited_marks: list[str] = []

    async def invoke(self, prompt: str) -> str:
        self._gauge.enter()
        try:
            for _ in range(3):
                await asyncio.sleep(0)
        finally:
            self._gauge.leave()
        self.exited_marks.append("B" if "SKILL-B-MARKER" in prompt else "other")
        return ""


class _OrderingRunner:
    """Gate runner keyed on the generated anchors; records ordering into ``events``."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.case_counts: list[int] = []

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        self._events.append("run")
        self.case_counts.append(len(cases))
        passing = {c.id for c in cases if c.id.endswith("::test_a")}
        if "IMPROVED-STEP" in view.text:
            passing |= {c.id for c in cases if c.id.endswith("::test_b")}
        return [EvalOutcome(case_id=c.id, passed=c.id in passing) for c in cases]


def _cfg(**overrides: Any) -> ImproverConfig:
    base: dict[str, Any] = {
        "min_traces": 1,
        "trace_buffer_turns": 0,
        "optimize_after_uses": 1,
        "max_iterations": 1,
        "eval_dimensions": ["accuracy"],
        "min_golden_cases": 1,
    }
    base.update(overrides)
    return ImproverConfig(**base)


async def _use(imp: ArcSkillImprover, skill: str, times: int = 1) -> None:
    """Record ``times`` failing traces for ``skill`` (one per turn)."""
    for _ in range(times):
        await imp.observe(skill_name=skill, tool_name="run", status="error", error_type="E")
        await imp.on_turn_end(turn=0, outcome="failure")


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0, msg: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            pytest.fail(msg)
        await asyncio.sleep(0.005)


# -- REQ-101: lazy trigger ----------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_trigger_generates_before_gate_decision(tmp_path: Path) -> None:
    """Suite-less skill + autogen on: generation (kind="create") runs BEFORE the gate,
    and the gate then decides on the generated cases — not on no_suite_policy."""
    skill_md = _make_skill(tmp_path, "s", with_suite=False)
    events: list[str] = []
    gen = _FakeSuiteGenerator(events=events)
    runner = _OrderingRunner(events)
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        llm=_ImprovingLLM(),
        eval_runner=runner,
        suite_generator=gen,
        skill_path=lambda name: skill_md,
    )
    await _use(imp, "s", times=2)
    await imp.maybe_improve()
    await imp.aclose()

    assert ("s", "create") in gen.calls
    assert "run" in events, "gate never ran on the generated suite"
    assert events.index("generate") < events.index("run"), "generation must precede the gate"
    assert runner.case_counts[0] == 3  # the gate read the 3 generated anchors


@pytest.mark.asyncio
async def test_lazy_trigger_disabled_when_autogen_off(tmp_path: Path) -> None:
    skill_md = _make_skill(tmp_path, "s", with_suite=False)
    gen = _FakeSuiteGenerator()
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(suite=SuiteConfig(autogen=False)),
        tier="personal",
        llm=_ImprovingLLM(),
        suite_generator=gen,
        skill_path=lambda name: skill_md,
    )
    await _use(imp, "s", times=2)
    await imp.maybe_improve()
    await imp.aclose()

    assert gen.calls == []


# -- REQ-108: per-skill single-flight -----------------------------------------


@pytest.mark.asyncio
async def test_generation_and_optimization_serialize_for_one_skill(tmp_path: Path) -> None:
    """A sweep-triggered generation and a maybe_improve-driven optimization for the SAME
    skill never overlap. The generator parks on an Event while the optimization is
    started and the loop is turned many times — without a per-skill lock, a second
    body (lazy generation or the LLM pass) enters concurrently and the gauge sees it."""
    skill_md = _make_skill(tmp_path, "s", with_suite=False)
    hold = asyncio.Event()
    gauge = _Gauge()
    gen = _FakeSuiteGenerator(hold=hold, gauge=gauge)
    llm = _GaugedLLM(gauge)
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        llm=llm,
        suite_generator=gen,
        skill_path=lambda name: skill_md,
        max_concurrent=4,
    )
    await _use(imp, "s", times=2)
    try:
        sweep = asyncio.create_task(imp.sweep_suites())
        await _wait_until(lambda: gen.entered >= 1, msg="sweep never started generation")

        await imp.maybe_improve()
        # Generous window: a lockless improver enters a second critical section here.
        for _ in range(30):
            await asyncio.sleep(0)
    finally:
        hold.set()
    await asyncio.wait_for(sweep, timeout=5)
    await imp.aclose()

    assert gauge.max_active == 1, (
        f"single-flight violated: {gauge.max_active} concurrent bodies for one skill"
    )


@pytest.mark.asyncio
async def test_two_skills_are_not_serialized_against_each_other(tmp_path: Path) -> None:
    """Locks are per-skill: skill B's optimization completes while skill A's generation
    is still parked in flight. A global lock would block B behind A and time out."""
    a_md = _make_skill(tmp_path, "a", with_suite=False)
    b_md = _make_skill(tmp_path, "b", with_suite=True, marker="SKILL-B-MARKER")
    paths = {"a": a_md, "b": b_md}
    hold = asyncio.Event()
    gen = _FakeSuiteGenerator(hold=hold)
    llm = _GaugedLLM(_Gauge())
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(optimize_after_uses=2),
        tier="personal",
        llm=llm,
        suite_generator=gen,
        skill_path=lambda name: paths.get(name),
        max_concurrent=4,
    )
    await _use(imp, "a", times=1)  # below optimize threshold; sweep target only
    await _use(imp, "b", times=3)  # over threshold; optimization target
    try:
        sweep = asyncio.create_task(imp.sweep_suites())
        await _wait_until(lambda: gen.entered >= 1, msg="sweep never started generation for a")

        await imp.maybe_improve()
        await _wait_until(
            lambda: "B" in llm.exited_marks,
            msg="skill b's optimization blocked behind skill a's in-flight generation",
        )
        assert gen.exited == 0, "skill a's generation should still be in flight"
    finally:
        hold.set()
    await asyncio.wait_for(sweep, timeout=5)
    await imp.aclose()


# -- REQ-106: post-mutation add-only extension ---------------------------------


@pytest.mark.asyncio
async def test_applied_prose_candidate_schedules_add_only_extension(tmp_path: Path) -> None:
    skill_md = _make_skill(tmp_path, "s", with_suite=True)
    anchor = skill_md.parent / "evals" / "test_g.py"
    anchor_bytes = anchor.read_bytes()
    events: list[str] = []
    gen = _FakeSuiteGenerator(events=events)
    reloaded: list[bool] = []
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        llm=_ImprovingLLM(),
        eval_runner=_OrderingRunner(events),
        suite_generator=gen,
        skill_path=lambda name: skill_md,
        reload=lambda: reloaded.append(True),
    )
    await _use(imp, "s", times=2)
    await imp.maybe_improve()
    await imp.aclose()

    assert reloaded == [True], "precondition: the prose candidate must apply"
    assert ("s", "extend") in gen.calls, "no add-only extension scheduled after mutation"
    assert ("s", "create") not in gen.calls  # suite existed; never regenerate from scratch
    assert anchor.read_bytes() == anchor_bytes, "adopted anchors must stay byte-identical"


# -- REQ-107: sweep backstop ---------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_generates_for_suiteless_skills_most_used_first(tmp_path: Path) -> None:
    a_md = _make_skill(tmp_path, "a", with_suite=False)
    b_md = _make_skill(tmp_path, "b", with_suite=False)
    paths = {"a": a_md, "b": b_md}
    gen = _FakeSuiteGenerator()
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(optimize_after_uses=100),
        tier="personal",
        suite_generator=gen,
        skill_path=lambda name: paths.get(name),
    )
    await _use(imp, "a", times=3)
    await _use(imp, "b", times=5)
    await asyncio.wait_for(imp.sweep_suites(), timeout=5)
    await imp.aclose()

    assert gen.calls == [("b", "create"), ("a", "create")]  # most-used-first


@pytest.mark.asyncio
async def test_sweep_early_exits_when_every_skill_has_a_suite(tmp_path: Path) -> None:
    a_md = _make_skill(tmp_path, "a", with_suite=True)
    b_md = _make_skill(tmp_path, "b", with_suite=True)
    paths = {"a": a_md, "b": b_md}
    gen = _FakeSuiteGenerator()
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(optimize_after_uses=100),
        tier="personal",
        suite_generator=gen,
        skill_path=lambda name: paths.get(name),
    )
    await _use(imp, "a", times=1)
    await _use(imp, "b", times=1)
    await asyncio.wait_for(imp.sweep_suites(), timeout=5)
    await imp.aclose()

    assert gen.calls == []


@pytest.mark.asyncio
async def test_sweep_does_not_double_claim_inflight_generation(tmp_path: Path) -> None:
    """A lazily-triggered generation is parked in flight; the sweep must not start a
    second generation for the same skill — one generate call total."""
    skill_md = _make_skill(tmp_path, "s", with_suite=False)
    hold = asyncio.Event()
    gen = _FakeSuiteGenerator(hold=hold)
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=_cfg(),
        tier="personal",
        llm=_ImprovingLLM(),
        suite_generator=gen,
        skill_path=lambda name: skill_md,
        max_concurrent=4,
    )
    await _use(imp, "s", times=2)
    try:
        await imp.maybe_improve()  # optimization lazily starts generation, parks on hold
        await _wait_until(lambda: gen.entered >= 1, msg="lazy trigger never started generation")

        sweep = asyncio.create_task(imp.sweep_suites())
        for _ in range(30):
            await asyncio.sleep(0.005)
        assert gen.entered == 1, "sweep double-claimed a skill with generation in flight"
    finally:
        hold.set()
    await asyncio.wait_for(sweep, timeout=5)
    await imp.aclose()

    assert gen.entered == 1
