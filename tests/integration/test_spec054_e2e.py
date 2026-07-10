"""SPEC-054 T-735 — E2E through the REAL wiring (the producers-unwired defense).

Drives the skill-improver eval-bootstrap chain front-to-back at personal tier with
the real production components. The ONLY scripted seam is the ``LLMInvoker``
(prompt-dispatched canned responses for suite generation, judge scoring, prose
reflection, and outcome classification). Everything else is real: the
``create_skill`` builtin, ``ArcSkillImprover``, ``SuiteGenerator``,
``HubEvalRunner`` (real Docker sandbox when available, else its own personal-tier
host fallback — the runner decides), ``EvalGate``, ``TraceStore``,
``CandidateStore``, the arcagent skills hooks, and arcstore ingestion.

Location: repo-root ``tests/integration/`` follows the cross-package SPEC-031
precedent (``test_spec031_e2e.py``); arcskill's own tests must not import
arcagent (the dependency DAG points the other way).

Stages (PLAN T-735):

1.  ``create_skill`` births fail-closed: ``evals/`` exists and is EMPTY,
    ``load_suite == []`` (REQ-105).
2/3. A real improver over real usage: ``observe``/``on_turn_end`` past the
    optimize threshold, then ``maybe_improve`` → the lazy trigger generates
    ``evals/test_golden_generated.py`` with the ``@generated`` docstring + a
    harness manifest entry; ``load_suite`` classifies the anchors
    ``machine_authored`` (REQ-101 through the real trigger).
4.  The gate DECISION consumes the anchors: a neutral prose candidate is
    rejected with 'no strict improvement' (never the no-suite policy), and a
    candidate applies ONLY when it flips a previously-failing anchor.
5.  Classifier producer: real hooks + a scripted eval-LLM persist a trace with
    ``task_outcome='failure'`` AND ``outcome_source='evaluator'`` (REQ-115).
6.  ``StoreIngest(workspace_dir=...)`` mirrors the applied candidate;
    ``query.skill_versions`` returns it active (REQ-120).
7.  ``rollback`` flips the manifest active id and appends an audit event.
8.  Repo hygiene: no placeholder eval scaffold remains anywhere under
    ``packages/*/src`` (REQ-105 regression guard).
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from arcagent.builtins.capabilities import _runtime as builtins_runtime
from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry, SkillEntry
from arcagent.modules.skills import _runtime as skills_runtime
from arcagent.modules.skills.capabilities import (
    skills_post_plan,
    skills_post_tool,
    skills_ready,
)
from arcagent.modules.skills.outcome import OutcomeClassifier
from arcskill.improver.config import ImproverConfig, SuiteConfig
from arcskill.improver.evalgate import load_suite
from arcskill.improver.improver import ArcSkillImprover
from arcstore import query
from arcstore.backends.memory import FakeBackend
from arcstore.ingest import StoreIngest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SKILL = "euro-totals"
_MARKER = "EURO-MODE"

_UTIL_PY = "def add(a, b):\n    return a + b\n"

# Body appended to the create_skill scaffold — gives the suite generator real
# Contract/Examples prose to oracle-ground against, and enough seed text that a
# one-line reflected addition stays inside the anchor-distance guardrail.
_SKILL_BODY = (
    "This skill sums transaction totals with scripts/util.py.\n"
    "Contract: add(a, b) returns the exact integer sum of a and b.\n"
    "Examples: add(2, 3) == 5; add(-1, 1) == 0.\n"
    "Validation: every arithmetic claim must be backed by an executable assertion.\n"
    "Totals are reported in the operator's requested currency.\n"
)

# Scripted suite-generation response: two well-formed, non-tautological
# candidates that pass the current bundle (scripts/ is on the harness sys.path)
# and fail the negative-control mutant (its import-time poison breaks `util`).
_SUITE_MODULE = """\
def test_add_contract():
    from util import add
    assert add(2, 3) == 5

def test_add_negative():
    from util import add
    assert add(-1, 1) == 0
"""

# Human-authored anchor that FAILS the current prose and passes once the
# improved prose documents the marker — the strict-improvement flip target.
_HUMAN_EVAL = f'''\
def test_prose_documents_euro_mode():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "SKILL.md").read_text(encoding="utf-8")
    assert text.count("{_MARKER}") >= 1
'''

_CORRECTION_TURN = [
    {"role": "assistant", "content": "I summed the totals in dollars."},
    {"role": "user", "content": "that's wrong — I asked for the totals in euros"},
]


class ScriptedLLM:
    """Prompt-dispatched ``LLMInvoker`` — the ONLY scripted seam in this module.

    Routes on the stable first line of each production prompt: suite generation
    (``SuiteGenerator._prompt``), judge (``SkillEvaluator.build_judge_prompt``),
    reflection (``SkillReflector.build_reflection_prompt``), and classification
    (``OutcomeClassifier._prompt``). Judge scores are content-based — a text
    carrying the improvement marker scores 5, anything else 1 — so the engine's
    frontier genuinely prefers the reflected candidate over the seed.
    """

    def __init__(self, *, suite_module: str = "") -> None:
        self.calls: list[str] = []
        self._suite_module = suite_module

    async def invoke(self, prompt: str) -> str:
        self.calls.append(prompt)
        if prompt.startswith("Generate pytest golden regression cases"):
            return self._suite_module
        if prompt.startswith("You are evaluating a skill procedure document"):
            score = 5 if _MARKER in prompt else 1
            return json.dumps({"checklist": [], "score": score, "rationale": "scripted"})
        if prompt.startswith("You are improving a skill procedure document"):
            current = prompt.partition("CURRENT SKILL:\n")[2]
            current = current.partition("\n\nFAILURE PATTERNS")[0]
            return f"```markdown\n{current.strip()}\n\n{_MARKER}: report all totals in euros.\n```"
        if prompt.startswith("Classify the outcome"):
            return json.dumps({"outcome": "failure", "skill": _SKILL})
        return "{}"


class _ListSink:
    """In-memory arctrust ``AuditSink`` (``write(event)``) recording every event."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)

    @property
    def actions(self) -> list[str]:
        return [event.action for event in self.events]


class _Ctx:
    """Minimal EventContext stand-in the skills hooks read (``data`` + veto flag)."""

    def __init__(self, **data: Any) -> None:
        self.data = data
        self.is_vetoed = False


@pytest.fixture(autouse=True)
def _reset_runtimes() -> Any:
    builtins_runtime.reset()
    skills_runtime.reset()
    yield
    builtins_runtime.reset()
    skills_runtime.reset()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Agent workspace with the builtins runtime configured for create_skill."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "capabilities").mkdir()
    builtins_runtime.configure(
        workspace=ws,
        loader=CapabilityLoader(
            scan_roots=[("workspace", ws / "capabilities")],
            registry=CapabilityRegistry(),
        ),
    )
    return ws


async def _create_skill(workspace: Path) -> Path:
    """Scaffold the skill through the real builtin; return its folder."""
    from arcagent.builtins.capabilities.create_skill import create_skill

    result = await create_skill(
        name=_SKILL,
        description="sum transaction totals",
        triggers=["sum totals"],
        tools=["read"],
        body=_SKILL_BODY,
    )
    assert f"Created skill '{_SKILL}'" in result
    return workspace / "capabilities/skills" / _SKILL


def _improver(
    workspace: Path, *, llm: ScriptedLLM, sink: _ListSink, skill_md: Path
) -> ArcSkillImprover:
    """A REAL improver: default (real) HubEvalRunner, real stores, scripted LLM only."""
    config = ImproverConfig(
        min_traces=2,
        trace_buffer_turns=0,
        optimize_after_uses=2,
        max_iterations=1,
        suite=SuiteConfig(
            min_cases=1,
            max_cases=2,
            candidate_budget=3,
            flake_runs=1,
            extend_after_mutation=False,
        ),
    )
    return ArcSkillImprover(
        workspace,
        config=config,
        tier="personal",
        llm=llm,
        audit_sink=sink,
        agent_did="did:arc:test:e2e",
        skill_path=lambda name: skill_md if name == _SKILL else None,
    )


async def _use_skill_past_threshold(improver: ArcSkillImprover, *, turns: int = 3) -> None:
    """Real usage signals: one clean tool call per turn, span closed each turn."""
    for turn in range(turns):
        await improver.observe(skill_name=_SKILL, tool_name="bash", status="ok", error_type=None)
        await improver.on_turn_end(turn=turn, outcome="")


def _no_swallowed_crash(caplog: pytest.LogCaptureFixture) -> None:
    """The guarded background pass logs-and-swallows; a crash there is a harness bug."""
    crashed = [r for r in caplog.records if "improvement pass failed" in r.message]
    assert not crashed, "background improvement pass crashed — see captured warnings"


# ---------------------------------------------------------------- stage 1 (REQ-105)


async def test_stage1_create_skill_births_fail_closed(workspace: Path) -> None:
    skill_dir = await _create_skill(workspace)
    evals_dir = skill_dir / "evals"
    assert evals_dir.is_dir()
    assert list(evals_dir.iterdir()) == []
    assert load_suite(skill_dir) == []


# ------------------------------------------------------ stages 2-4a (REQ-101/108)


async def test_stage2_4_suite_generated_via_real_trigger_and_gate_consumes_it(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """maybe_improve's lazy trigger must REALLY produce anchors, and the gate must
    decide on those anchors (rejection reason 'no strict improvement'), never fall
    through to the no-suite policy."""
    caplog.set_level(logging.INFO)
    skill_dir = await _create_skill(workspace)
    (skill_dir / "scripts" / "util.py").write_text(_UTIL_PY, encoding="utf-8")
    skill_md = skill_dir / "SKILL.md"
    seed_text = skill_md.read_text(encoding="utf-8")

    llm = ScriptedLLM(suite_module=_SUITE_MODULE)
    sink = _ListSink()
    improver = _improver(workspace, llm=llm, sink=sink, skill_md=skill_md)

    await _use_skill_past_threshold(improver)
    await improver.maybe_improve()
    await improver.aclose()

    _no_swallowed_crash(caplog)

    # REQ-101: the generated suite exists, marked and manifested (stage 3).
    generated = skill_dir / "evals" / "test_golden_generated.py"
    assert generated.exists(), (
        "REAL-PATH GAP: SuiteGenerator adopted nothing — the candidate case source "
        "is never materialized into the sandbox bundle, so every candidate fails "
        "its current-bundle run and quarantines (suitegen._cascade_verdict runs "
        "cases whose evals/test_golden_generated.py does not exist yet)"
    )
    module = ast.parse(generated.read_text(encoding="utf-8"))
    assert "@generated" in (ast.get_docstring(module) or "")
    manifest = json.loads((skill_dir / "evals" / ".manifest.json").read_text(encoding="utf-8"))
    assert "test_golden_generated.py" in manifest["files"]
    cases = load_suite(skill_dir)
    assert cases, "generated anchors must be discoverable by load_suite"
    assert all(case.machine_authored for case in cases)

    # Stage 4a: the gate decision CONSUMED the generated suite. A neutral prose
    # candidate (anchors exercise scripts, not prose) is rejected for lack of
    # strict improvement — and the fail-open no-suite policy never ran.
    assert not [r for r in caplog.records if "no golden suite" in r.message], (
        "gate fell through to no_suite_policy — anchors did not gate the candidate"
    )
    assert [r for r in caplog.records if "no strict improvement" in r.message], (
        "expected the eval gate to reject the neutral prose candidate on the "
        "generated anchors with 'no strict improvement'"
    )
    assert skill_md.read_text(encoding="utf-8") == seed_text, (
        "rejected candidate must leave SKILL.md untouched"
    )


# --------------------------------------------- stages 4b + 6 + 7 (REQ-115/119/120)


async def test_stage4_6_7_strict_improvement_applies_then_arcstore_and_rollback(
    workspace: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A candidate applies ONLY by flipping a previously-failing anchor through the
    real runner; the applied version is queryable via arcstore; rollback flips the
    manifest and is audited."""
    caplog.set_level(logging.INFO)
    skill_dir = await _create_skill(workspace)
    (skill_dir / "scripts" / "util.py").write_text(_UTIL_PY, encoding="utf-8")
    (skill_dir / "evals" / "test_human.py").write_text(_HUMAN_EVAL, encoding="utf-8")
    skill_md = skill_dir / "SKILL.md"

    llm = ScriptedLLM(suite_module=_SUITE_MODULE)
    sink = _ListSink()
    improver = _improver(workspace, llm=llm, sink=sink, skill_md=skill_md)

    await _use_skill_past_threshold(improver)
    await improver.maybe_improve()
    await improver.aclose()

    _no_swallowed_crash(caplog)

    # Accepted ONLY via strict improvement over the real sandbox runs — the
    # fail-open no-suite path never ran, and the flip target is now documented.
    assert not [r for r in caplog.records if "no golden suite" in r.message]
    assert _MARKER in skill_md.read_text(encoding="utf-8"), (
        "strict-improvement candidate was not applied — the previously-failing "
        "human anchor should have flipped fail->pass through the real eval runner"
    )
    manifest_path = workspace / "skill_traces" / _SKILL / "candidates" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    applied_id = manifest["active_candidate_id"]
    assert applied_id and applied_id != "seed"
    assert "skill.mutation.applied" in sink.actions

    # Stage 6 (REQ-120): arcstore mirrors the applied candidate from disk.
    backend = FakeBackend()
    ingest = StoreIngest(
        backend,
        spool_dir=tmp_path / "spool",
        worm_dir=tmp_path / "worm",
        workspace_dir=workspace,
    )
    await ingest.scan_once()
    versions = await query.skill_versions(backend, _SKILL)
    active = [v for v in versions if v["active"]]
    assert [v["candidate_id"] for v in active] == [applied_id]

    # Stage 7: rollback flips the manifest active id and appends an audit event.
    improver.rollback(_SKILL, "seed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["active_candidate_id"] == "seed"
    assert "skill.mutation.rolled_back" in sink.actions

    # The flip is visible on re-ingest: the applied candidate is no longer active.
    # ("seed" itself carries no manifest candidates entry, so no seed row exists.)
    await ingest.scan_once()
    versions = await query.skill_versions(backend, _SKILL)
    by_id = {v["candidate_id"]: v for v in versions}
    assert by_id[applied_id]["active"] is False
    assert all(not v["active"] for v in versions)


# ------------------------------------------------------------- stage 5 (REQ-115)


async def test_stage5_classifier_label_lands_in_persisted_trace(workspace: Path) -> None:
    """The REQ-115 producer end-to-end: real hooks -> real OutcomeClassifier (scripted
    eval-LLM seam) -> real ArcSkillImprover adapter -> persisted trace JSONL with
    task_outcome='failure' AND outcome_source='evaluator'."""
    skill_dir = await _create_skill(workspace)
    skill_md = skill_dir / "SKILL.md"

    skills_runtime.configure(
        config={"adapter": "arcskill", "classify_outcomes": True}, workspace=workspace
    )
    state = skills_runtime.state()
    assert state.active, "arcskill adapter must be selected and live"
    assert state.outcome_classifier is not None, "classify_outcomes=True must build one"
    # Real OutcomeClassifier over the scripted eval-LLM seam: configure() built one
    # over get_eval_model's result, which is None here (no eval model configured in
    # this environment) and would abstain on every turn.
    cls_llm = ScriptedLLM()
    state.outcome_classifier = OutcomeClassifier(llm=cls_llm)

    registry = CapabilityRegistry()
    await registry.register_skill(
        SkillEntry(
            name=_SKILL,
            version="1.0.0",
            description="sum transaction totals",
            triggers=(),
            tools=(),
            location=skill_md,
            scan_root="workspace",
        )
    )
    await skills_ready(_Ctx(skill_registry=registry))
    await skills_post_tool(_Ctx(tool="read", args={"file_path": str(skill_md)}))
    await skills_post_tool(_Ctx(tool="bash", args={"cmd": "python totals.py"}))
    await skills_post_plan(_Ctx(turn_number=1, messages=_CORRECTION_TURN))

    classify_calls = [p for p in cls_llm.calls if p.startswith("Classify the outcome")]
    assert len(classify_calls) == 1, "correction turn must trigger exactly one eval-LLM call"

    trace_files = sorted((workspace / "skill_traces" / _SKILL).glob("traces-*.jsonl"))
    assert trace_files, "turn close must persist the skill span as trace JSONL"
    traces = [
        json.loads(line)
        for path in trace_files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert traces[-1]["task_outcome"] == "failure"
    assert traces[-1]["outcome_source"] == "evaluator"


# ------------------------------------------------------------- stage 8 (REQ-105)


def _non_docstring_string_literals(path: Path) -> list[str]:
    """Every string literal in ``path`` that is not a module/class/function docstring."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_stage8_no_placeholder_eval_scaffold_in_any_package_src() -> None:
    """REQ-105 regression guard: no src module carries an eval scaffold that writes
    ``assert True`` placeholders, and the old ``evals/test_golden.py`` scaffold
    constant is gone (docstring mentions are prose, not scaffolds)."""
    create_skill_src = (
        _REPO_ROOT / "packages/arcagent/src/arcagent/builtins/capabilities/create_skill.py"
    )
    assert "assert True" not in create_skill_src.read_text(encoding="utf-8")

    offenders: list[str] = []
    for src_file in sorted(_REPO_ROOT.glob("packages/*/src/**/*.py")):
        for literal in _non_docstring_string_literals(src_file):
            if "assert True" in literal or "evals/test_golden.py" in literal:
                offenders.append(str(src_file.relative_to(_REPO_ROOT)))
                break
    assert offenders == []
