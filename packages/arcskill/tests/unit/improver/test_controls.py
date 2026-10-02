"""alpha-2 P8 — operator controls on the improver: read model, improve-now, golden set.

Pins the three operator surfaces the arcui routes and the CLI drive:

* :class:`ImproverStateReader` — a write-free read model over the candidate store,
  the trace store, the lifecycle manifest, the gate log and the golden suite.
* ``ArcSkillImprover.improve_now`` — the SAME optimize pass the usage trigger runs.
  ``dry_run`` stops before ``apply_result`` and returns the diff + gate verdict with
  no write; a real apply re-runs the SAME ``EvalGate`` and ``_authorize`` before
  ``apply_result``.
* ``run_evals`` / ``regen_evals`` — run the golden suite now; regenerate the
  machine-authored anchors for real (refused when a human edited them).

Every call is bounded (timeout, single-flight per skill) and audited.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.gate_log import GateLog, GateRecord
from arcskill.improver.models import BundleView, Candidate, EvalCase, EvalOutcome, OptimizeResult
from arcskill.improver.reader import ImproverStateReader

from packages.arcskill.tests.conftest import DirRevisionWriter

_SEED = "---\nname: s\n---\n# s\n\nDo the thing.\n"
_BETTER = "---\nname: s\n---\n# s\n\nDo the thing carefully.\n"
_CASES = "def test_a():\n    assert 1\n\ndef test_b():\n    assert 1\n"


class _Runner:
    """Fake sandbox: ``test_b`` passes only on the improved text."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        self.calls += 1
        better = "carefully" in view.text
        return [EvalOutcome(case_id=c.id, passed=c.id.endswith("test_a") or better) for c in cases]


class _RegressRunner:
    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        return [EvalOutcome(case_id=c.id, passed="carefully" not in view.text) for c in cases]


class _LLM:
    async def invoke(self, prompt: str) -> str:
        return ""


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [e.action for e in self.events]


class _Trigger:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, *, skill_name: str, skill_dir: Path, kind: str) -> None:
        self.calls.append(kind)
        evals = skill_dir / "evals"
        evals.mkdir(exist_ok=True)
        (evals / "test_golden_generated.py").write_text(
            '"""@generated golden anchors."""\n\ndef test_new():\n    assert 1\n',
            encoding="utf-8",
        )


def _skill(root: Path, *, suite: bool = True) -> Path:
    sk = root / "skills" / "s"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(_SEED, encoding="utf-8")
    if suite:
        (sk / "evals").mkdir()
        (sk / "evals" / "test_g.py").write_text(_CASES, encoding="utf-8")
    return sk / "SKILL.md"


def _improver(
    tmp_path: Path,
    skill_md: Path | None,
    *,
    tier: str = "personal",
    runner: Any = None,
    llm: Any = "default",
    sink: Any = None,
    approver: Any = None,
    trigger: Any = None,
    config: ImproverConfig | None = None,
) -> ArcSkillImprover:
    return ArcSkillImprover(
        tmp_path / "ws",
        config=config or ImproverConfig(),
        tier=tier,
        llm=_LLM() if llm == "default" else llm,
        eval_runner=runner or _Runner(),
        audit_sink=sink,
        approval_provider=approver,
        suite_generator=trigger,
        agent_did="did:arc:test:agent",
        writer=DirRevisionWriter(lambda _n: skill_md.parent if skill_md else Path()),
        skill_path=lambda name: skill_md if name == "s" else None,
    )


async def _traces(imp: ArcSkillImprover, count: int = 3) -> None:
    for turn in range(count):
        await imp.observe(
            skill_name="s",
            tool_name="bash",
            status="ok",
            error_type=None,
            call_id=f"c{turn}",
            run_id=f"r{turn}",
        )
        await imp.on_turn_end(turn=turn, outcome="", run_id=f"r{turn}")


@pytest.fixture
def proposes_better(monkeypatch: pytest.MonkeyPatch) -> None:
    """The optimize pass (judge + reflector) proposes ``_BETTER`` — the engine is
    covered in test_engine.py; here it is the seam the controls reuse."""

    async def optimize(
        self: Any, skill_name: str, current_text: str, traces: list[Any], **kwargs: Any
    ) -> OptimizeResult:
        best = Candidate(
            id="abc123def456",
            text=_BETTER,
            aggregate_scores={"accuracy": 4.0},
            parent_id="seed",
            generation=1,
        )
        return OptimizeResult(
            skill_name=skill_name,
            best_candidate=best,
            frontier=[best],
            iterations_run=1,
            stop_reason="max_iterations",
            seed_scores={"accuracy": 3.0},
            improvement={"accuracy": 1.0},
        )

    monkeypatch.setattr("arcskill.improver.engine.SkillOptimizer.optimize", optimize)


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# improve_now — dry run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_dry_run_returns_diff_and_gate_verdict_and_writes_nothing(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    sink = _Sink()
    imp = _improver(tmp_path, skill_md, sink=sink)
    await _traces(imp)
    before = _files(tmp_path)

    result = await imp.improve_now(skill_name="s", dry_run=True)

    assert result["status"] == "preview"
    assert result["gate"]["accepted"] is True
    assert result["gate"]["newly_passing"] == 1
    assert "+Do the thing carefully." in result["diff"]
    assert "-Do the thing." in result["diff"]
    assert result["candidate_id"] == "abc123def456"
    assert result["preview_id"]
    assert result["scores"] == {"accuracy": 4.0}
    assert _files(tmp_path) == before, "a dry run must not write anything"
    assert "skill.improve_now.preview" in sink.actions()


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_dry_run_reports_a_gate_rejection_with_its_reason(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md, runner=_RegressRunner())
    await _traces(imp)

    result = await imp.improve_now(skill_name="s", dry_run=True)

    assert result["status"] == "preview"
    assert result["gate"]["accepted"] is False
    assert "regression" in result["gate"]["reason"]


# ---------------------------------------------------------------------------
# improve_now — apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_apply_of_a_preview_runs_the_gate_again_and_applies(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    sink = _Sink()
    runner = _Runner()
    imp = _improver(tmp_path, skill_md, sink=sink, runner=runner)
    await _traces(imp)
    preview = await imp.improve_now(skill_name="s", dry_run=True)
    gate_runs = runner.calls

    result = await imp.improve_now(skill_name="s", dry_run=False, preview_id=preview["preview_id"])

    assert result["status"] == "applied"
    assert result["candidate_id"] == "abc123def456"
    assert runner.calls > gate_runs, "apply must re-run the SAME eval gate"
    assert skill_md.read_text(encoding="utf-8") == _BETTER
    assert "skill.mutation.applied" in sink.actions()
    assert "skill.improve_now.apply" in sink.actions()
    log = GateLog(tmp_path / "ws").recent("s")
    assert log[0].outcome == "applied"
    assert log[0].source == "manual"


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_apply_without_a_preview_runs_a_fresh_pass(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md)
    await _traces(imp)

    result = await imp.improve_now(skill_name="s", dry_run=False)

    assert result["status"] == "applied"
    assert skill_md.read_text(encoding="utf-8") == _BETTER


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_apply_refuses_a_preview_of_a_skill_that_changed_since(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md)
    await _traces(imp)
    preview = await imp.improve_now(skill_name="s", dry_run=True)
    skill_md.write_text(_SEED + "\nOperator edit.\n", encoding="utf-8")

    result = await imp.improve_now(skill_name="s", dry_run=False, preview_id=preview["preview_id"])

    assert result["status"] == "stale_preview"
    assert "Operator edit." in skill_md.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_apply_of_an_unknown_preview_is_expired(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md)

    result = await imp.improve_now(skill_name="s", dry_run=False, preview_id="nope")

    assert result["status"] == "preview_expired"
    assert skill_md.read_text(encoding="utf-8") == _SEED


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_a_preview_is_applied_at_most_once(tmp_path: Path) -> None:
    """Abuse case: a replayed apply of a consumed preview finds nothing to apply."""
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md)
    await _traces(imp)
    preview = await imp.improve_now(skill_name="s", dry_run=True)
    first = await imp.improve_now(skill_name="s", dry_run=False, preview_id=preview["preview_id"])

    replay = await imp.improve_now(skill_name="s", dry_run=False, preview_id=preview["preview_id"])

    assert first["status"] == "applied"
    assert replay["status"] == "preview_expired"


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_a_preview_of_one_skill_cannot_be_applied_to_another(tmp_path: Path) -> None:
    """Abuse case: a preview id is bound to the skill it was made for."""
    skill_md = _skill(tmp_path)
    other = tmp_path / "skills" / "t" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text(_SEED, encoding="utf-8")
    imp = ArcSkillImprover(
        tmp_path / "ws",
        llm=_LLM(),
        eval_runner=_Runner(),
        skill_path=lambda name: {"s": skill_md, "t": other}.get(name),
    )
    await _traces(imp)
    preview = await imp.improve_now(skill_name="s", dry_run=True)

    result = await imp.improve_now(skill_name="t", dry_run=False, preview_id=preview["preview_id"])

    assert result["status"] == "preview_expired"
    assert other.read_text(encoding="utf-8") == _SEED


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_apply_rejected_by_the_gate_writes_nothing_and_logs_the_reason(
    tmp_path: Path,
) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md, runner=_RegressRunner())
    await _traces(imp)

    result = await imp.improve_now(skill_name="s", dry_run=False)

    assert result["status"] == "rejected"
    assert "regression" in result["gate"]["reason"]
    assert skill_md.read_text(encoding="utf-8") == _SEED
    log = GateLog(tmp_path / "ws").recent("s")
    assert log[0].outcome == "rejected"
    assert "regression" in log[0].reason


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_federal_apply_without_an_approver_is_denied_fail_closed(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    sink = _Sink()
    imp = _improver(tmp_path, skill_md, tier="federal", sink=sink)
    await _traces(imp)

    result = await imp.improve_now(skill_name="s", dry_run=False)

    assert result["status"] == "denied"
    assert skill_md.read_text(encoding="utf-8") == _SEED
    assert "skill.mutation.approval" in sink.actions()


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_federal_apply_consults_the_approver(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    asked: list[str] = []

    async def approve(action: str, skill_name: str, detail: str) -> bool:
        asked.append(action)
        return True

    imp = _improver(tmp_path, skill_md, tier="federal", approver=approve)
    await _traces(imp)

    result = await imp.improve_now(skill_name="s", dry_run=False)

    assert result["status"] == "applied"
    assert asked == ["skill.mutation"]


# ---------------------------------------------------------------------------
# improve_now — refusals and bounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_skill_is_not_found(tmp_path: Path) -> None:
    imp = _improver(tmp_path, _skill(tmp_path))
    result = await imp.improve_now(skill_name="ghost", dry_run=True)
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_without_an_eval_model_improvement_is_unavailable(tmp_path: Path) -> None:
    imp = _improver(tmp_path, _skill(tmp_path), llm=None)
    result = await imp.improve_now(skill_name="s", dry_run=True)
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_too_few_traces_is_reported_not_attempted(tmp_path: Path) -> None:
    imp = _improver(tmp_path, _skill(tmp_path))
    await _traces(imp, count=1)
    result = await imp.improve_now(skill_name="s", dry_run=True)
    assert result["status"] == "insufficient_traces"


@pytest.mark.asyncio
async def test_an_exempt_skill_is_never_improved(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    skill_md.write_text("---\nname: s\ntags: [auth]\n---\n# s\n", encoding="utf-8")
    imp = _improver(tmp_path, skill_md)
    await _traces(imp)
    result = await imp.improve_now(skill_name="s", dry_run=True)
    assert result["status"] == "exempt"


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_a_pass_already_running_for_the_skill_is_busy(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md)
    await _traces(imp)
    async with imp._skill_lock("s"):
        result = await imp.improve_now(skill_name="s", dry_run=True)
    assert result["status"] == "busy"


@pytest.mark.asyncio
async def test_a_pass_over_its_time_budget_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(self: Any, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(5)

    monkeypatch.setattr("arcskill.improver.engine.SkillOptimizer.optimize", slow)
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md, config=ImproverConfig(manual_timeout_s=0.05))
    await _traces(imp)

    result = await imp.improve_now(skill_name="s", dry_run=True)

    assert result["status"] == "timeout"
    assert skill_md.read_text(encoding="utf-8") == _SEED


# ---------------------------------------------------------------------------
# run_evals / regen_evals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_evals_reports_pass_fail_per_case_and_audits(tmp_path: Path) -> None:
    sink = _Sink()
    imp = _improver(tmp_path, _skill(tmp_path), sink=sink)

    result = await imp.run_evals(skill_name="s")

    assert result["status"] == "completed"
    assert result["total"] == 2
    assert result["passed"] == 1
    assert result["failed"] == 1
    by_id = {c["case_id"]: c for c in result["cases"]}
    assert by_id["evals/test_g.py::test_a"]["passed"] is True
    assert by_id["evals/test_g.py::test_b"]["passed"] is False
    assert by_id["evals/test_g.py::test_a"]["provenance"] == "human"
    assert "skill.evals.run" in sink.actions()


@pytest.mark.asyncio
async def test_run_evals_without_a_suite_says_so(tmp_path: Path) -> None:
    imp = _improver(tmp_path, _skill(tmp_path, suite=False))
    result = await imp.run_evals(skill_name="s")
    assert result["status"] == "no_suite"


@pytest.mark.asyncio
async def test_regen_really_regenerates_the_machine_suite_and_audits(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    sink = _Sink()
    trigger = _Trigger()
    imp = _improver(tmp_path, skill_md, sink=sink, trigger=trigger)

    result = await imp.regen_evals(skill_name="s")

    assert result["status"] == "completed"
    assert trigger.calls == ["regen"]
    assert (skill_md.parent / "evals" / "test_golden_generated.py").is_file()
    assert "skill.evals.regen" in sink.actions()


@pytest.mark.asyncio
async def test_regen_refuses_to_overwrite_a_human_edited_generated_file(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    evals = skill_md.parent / "evals"
    generated = evals / "test_golden_generated.py"
    machine = '"""@generated golden anchors."""\n\ndef test_old():\n    assert 1\n'
    generated.write_text(machine, encoding="utf-8")
    digest = hashlib.sha256(machine.encode("utf-8")).hexdigest()
    (evals / ".manifest.json").write_text(
        json.dumps({"files": {"test_golden_generated.py": {"sha256": digest}}}), encoding="utf-8"
    )
    generated.write_text(machine + "\ndef test_mine():\n    assert 2\n", encoding="utf-8")
    trigger = _Trigger()
    imp = _improver(tmp_path, skill_md, trigger=trigger)

    result = await imp.regen_evals(skill_name="s")

    assert result["status"] == "refused"
    assert trigger.calls == []
    assert "test_mine" in generated.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_regen_without_an_eval_model_is_unavailable(tmp_path: Path) -> None:
    imp = _improver(tmp_path, _skill(tmp_path), llm=None)
    result = await imp.regen_evals(skill_name="s")
    assert result["status"] == "unavailable"


# ---------------------------------------------------------------------------
# ImproverStateReader — the read model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("proposes_better")
async def test_reader_shows_state_candidates_scores_and_gate_verdicts(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    imp = _improver(tmp_path, skill_md)
    await _traces(imp)
    await imp.improve_now(skill_name="s", dry_run=False)

    state = ImproverStateReader(tmp_path / "ws").read("s", skill_dir=skill_md.parent)
    data = state.to_dict()

    assert data["skill_name"] == "s"
    assert data["lifecycle_state"] == "active"
    assert data["active_candidate_id"] == "abc123def456"
    assert data["traces"]["total"] == 3
    assert data["candidates"][0]["candidate_id"] == "abc123def456"
    assert data["candidates"][0]["scores"] == {"accuracy": 4.0}
    assert data["candidates"][0]["active"] is True
    assert data["gate_log"][0]["outcome"] == "applied"
    assert data["gate_log"][0]["reason"]
    assert data["suite"]["total"] == 2
    assert data["suite"]["human"] == 2


def test_reader_of_an_untouched_skill_is_empty_and_writes_nothing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    data = ImproverStateReader(ws).read("never-seen").to_dict()

    assert data["lifecycle_state"] == "active"
    assert data["candidates"] == []
    assert data["gate_log"] == []
    assert data["traces"]["total"] == 0
    assert not any(ws.rglob("*")), "a read must not create directories"


def test_reader_refuses_a_path_traversal_skill_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ImproverStateReader(tmp_path).read("../escape")


def test_gate_log_is_bounded(tmp_path: Path) -> None:
    log = GateLog(tmp_path)
    for i in range(520):
        log.record(
            GateRecord(
                skill_name="s",
                source="auto",
                kind="prose",
                accepted=False,
                reason=f"r{i}",
                outcome="rejected",
            )
        )
    lines = log.path("s").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 500
    assert log.recent("s", limit=1)[0].reason == "r519"
