"""End-to-end smoke — T-822 / COMP-005, COMP-007, COMP-008, COMP-017.

Covers REQ-181 (gold-evidence damage voids the question) and REQ-207 (the live
end-to-end path is proven before a phase is trusted), and asserts the property
REQ-195/REQ-196 exist for: a run leaves nothing git can see.

*The file is split by what can be proven without spending money, not by what is
convenient.* Two of the three assertions in T-822's acceptance need neither the
dataset nor a provider key, so they run unconditionally:

- the gold-evidence void, driven through the REAL ``SanitizeFidelityGate``, the
  REAL ``IngestDriver`` and a REAL ``ArcAgent`` — the gate screens a session's
  chunks before any of them is fed, so damage in the first session voids before
  a single LLM call and the whole path is exercisable offline;
- repo cleanliness, asserted after writing the harness's real artifacts (agent
  TOMLs, scaffolded workspace, results JSONL, cost ledger, ``run_manifest.json``)
  at their real default locations inside this repository.

The third — three questions spanning types through ingest, consolidation, query
and judge — needs the corpus and two provider keys, so it is marked ``slow`` and
skipped by name when either is missing. **A skip here is not a pass.** The skip
reason names exactly what is absent so nobody reads the one for the other.

``slow`` is the repository's existing marker for "end-to-end test that needs a
resource the developer may not have installed" (``tests/integration/
test_spec031_e2e.py``, ``packages/arcrun/tests/integration/*_live.py``). Its
registered description in the root ``pyproject.toml`` names ``nats-server``
specifically, which under-describes what the marker is actually used for; that
wording is worth widening, but this file does not edit configuration to do it.

*Nothing is mocked.* ``arcmemory.security`` in particular is imported live
through the gate: a stub would pin this spec's guess about what the filters
destroy rather than what they destroy, and would keep passing on the day
arcmemory's patterns change — which is the exact failure REQ-181 exists to
catch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

import pytest
from arcstore.config import ENV_DATA_DIR

from evaluations.ingest.agent_factory import build_eval_agent
from evaluations.ingest.chunker import TurnChunker
from evaluations.ingest.consolidation import ConsolidationResult, ConsolidationWaiter
from evaluations.ingest.fidelity import SanitizeFidelityGate
from evaluations.ingest.lifecycle import leftover_workspaces
from evaluations.ingest.types import Session, Turn
from evaluations.longmemeval import cli
from evaluations.longmemeval.adapter import LongMemEvalAdapter
from evaluations.longmemeval.budget import (
    CURRENT_PRICING_TABLE_VERSION,
    MAX_EVENT_CHARS,
    BudgetGovernor,
    CallProfile,
    estimate_run,
)
from evaluations.longmemeval.dataset import Dataset, load_dataset
from evaluations.longmemeval.hygiene import RepoHygieneGuard
from evaluations.longmemeval.judge import JUDGE_API_KEY_ENV
from evaluations.longmemeval.ledger import ResultLedger
from evaluations.longmemeval.preflight import PREFLIGHT_QUESTION_ID
from evaluations.longmemeval.runner import (
    ConsolidationSession,
    PhaseReport,
    PhaseRunner,
    WaiterBuilder,
)

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = cli.RUNS_ROOT
RESULTS_ROOT = cli.RESULTS_DIR
ORACLE_DATASET = cli.DATA_DIR / cli.PHASE_DATASETS["oracle"]

AGENT_API_KEY_ENV = "ANTHROPIC_API_KEY"
"""The eval agent's model AND its distiller are both ``anthropic/...``."""

SMOKE_QUESTIONS = 3
"""T-822's three questions. ``select_smoke_questions`` takes one type each."""

GIT = shutil.which("git")

# Ordinary benchmark prose the live filters destroy: it trips ``_INJECTION_RE``'s
# "you are now" branch, which deletes its match plus everything to end of line.
# Marked as this question's gold evidence, so the damage is unscoreable.
INJECTION_TURN = "Congratulations! You are now a certified PM as of May 2023."

_OFFLINE_QUESTION_ID = "smoke-e2e-offline"

_REPO_ARTIFACT_PREFIXES = (
    "evaluations/",
    # The repo-root guards: `ArcAgent(cfg)` without an absolute `config_path`
    # resolves its workspace, traces, audit chain and capability scan root
    # against the process CWD, which under pytest is this repository root.
    "workspace/",
    "traces/",
    ".audit/",
    "capabilities/",
    "arcagent.toml",
    "arcllm.toml",
    "arcrun.toml",
)
"""Every path a harness run could write. A `git status` line outside these is
someone else's edit, not a leak — and this repository is worked on concurrently,
so a whole-tree delta would fail for reasons that have nothing to do with T-822.
"""


# ---------------------------------------------------------------------------
# Skip conditions — each names what is missing, so a skip cannot read as a pass
# ---------------------------------------------------------------------------


def _live_run_blockers() -> list[str]:
    """Everything the live end-to-end run needs and does not have."""
    missing: list[str] = []
    if not ORACLE_DATASET.is_file():
        missing.append(
            f"the LongMemEval oracle corpus is not downloaded ({ORACLE_DATASET} is "
            "absent; evaluations/data/ is gitignored)"
        )
    for variable, purpose in (
        (AGENT_API_KEY_ENV, "the eval agent's model and its distiller"),
        (JUDGE_API_KEY_ENV, "the judge"),
    ):
        if not os.environ.get(variable):
            missing.append(f"${variable} is unset ({purpose})")
    return missing


_LIVE_BLOCKERS = _live_run_blockers()

requires_live_stack = pytest.mark.skipif(
    bool(_LIVE_BLOCKERS),
    reason="live end-to-end run not attempted: " + "; ".join(_LIVE_BLOCKERS),
)
requires_git = pytest.mark.skipif(GIT is None, reason="git is not on PATH")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HOME, the Arc config root and arcstore at throwaway directories.

    The developer's ``~/.arc/arcagent.toml`` merges under every per-agent config,
    so an unisolated run would build an agent from settings the harness never
    wrote. ``ARCSTORE_DATA_DIR`` outranks the emitted ``[arcstore] data_dir``, so
    it is pinned here for ``monkeypatch`` to restore afterwards — ``build_eval_agent``
    sets it per run directory as a process-wide side effect.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-config"))
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "arcstore-placeholder"))


@pytest.fixture
def no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the provider keys, so an offline test cannot quietly spend money.

    ``assert session_keys == []`` proves the harness did not *intend* a call;
    removing the keys is what makes it impossible for one to succeed if that
    assertion is ever weakened or a screening order changes underneath it.
    """
    for variable in (AGENT_API_KEY_ENV, JUDGE_API_KEY_ENV, "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def repo_results_dir(request: pytest.FixtureRequest) -> Iterator[Path]:
    """A results directory at the harness's REAL default location in this repo.

    ``tmp_path`` would prove nothing about repo hygiene: ``.gitignore`` anchors
    ``/evaluations/results/`` and ``/evaluations/runs/``, so only artifacts
    written *there* exercise the patterns. Teardown removes them, and the
    preflight's scratch workspace too — COMP-020 deliberately never deletes it.

    ``runs/`` is created up front because ``/evaluations/runs/`` carries a
    trailing slash, so ``git check-ignore`` reports a path that does not exist
    yet as NOT ignored and ``RepoHygieneGuard`` refuses the run. That is a real
    first-run defect in the guard's wiring, not something this test may fix.
    """
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    results_dir = RESULTS_ROOT / f"smoke-e2e-{request.function.__name__}"
    try:
        yield results_dir
    finally:
        shutil.rmtree(results_dir, ignore_errors=True)
        shutil.rmtree(RUNS_ROOT / PREFLIGHT_QUESTION_ID, ignore_errors=True)
        for leftover in leftover_workspaces(RUNS_ROOT):
            if leftover.name.startswith(_OFFLINE_QUESTION_ID):
                shutil.rmtree(leftover, ignore_errors=True)


# ---------------------------------------------------------------------------
# Observation wrappers — real collaborators, with what they did recorded
# ---------------------------------------------------------------------------


class _RecordingWaiter:
    """The real :class:`ConsolidationWaiter`, with every pass it fires recorded.

    Wrapping rather than replacing is the point: the pass, its markers and its
    stall check are arcmemory's and COMP-008's, and only the observation is
    added — ``ConsolidationResult`` is returned by ``wait()`` and otherwise
    discarded by the phase runner, so there is nowhere else to see it from.
    """

    def __init__(
        self, waiter: ConsolidationWaiter, recorded: list[ConsolidationResult]
    ) -> None:
        self._waiter = waiter
        self._recorded = recorded

    @property
    def warnings(self) -> Sequence[str]:
        return self._waiter.warnings

    def __enter__(self) -> _RecordingWaiter:
        self._waiter.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._waiter.__exit__(exc_type, exc, tb)

    async def wait(self) -> ConsolidationResult:
        result = await self._waiter.wait()
        self._recorded.append(result)
        return result


def _recording_waiter_builder(recorded: list[ConsolidationResult]) -> WaiterBuilder:
    """A ``WaiterBuilder`` that hands out recording wrappers over the real waiter."""

    def build(*, agent: Any, workspace: Path) -> ConsolidationSession:
        return _RecordingWaiter(ConsolidationWaiter(agent=agent, workspace=workspace), recorded)

    return build


class _CountingAgent:
    """The real eval agent, with every turn the harness spends on it recorded.

    Exists so "the void cost nothing" is an assertion rather than a claim: the
    fidelity gate screens the whole question before the driver feeds anything,
    and an empty call list is what proves it.
    """

    def __init__(self, agent: ArcAgent, session_keys: list[str]) -> None:
        self._agent = agent
        self._session_keys = session_keys
        self._capability_registry = agent._capability_registry
        self._workspace = agent._workspace

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else, so the wrapper stays invisible to the harness.

        The consolidation waiter reaches for ``_runtime_bindings`` and the phase
        runner for ``_workspace``; enumerating that list here would make this
        wrapper a second, drifting definition of what an agent is.
        """
        return getattr(self._agent, name)

    async def run_collected(self, input_text: str, *, session_key: str) -> Any:
        self._session_keys.append(session_key)
        return await self._agent.run_collected(input_text, session_key=session_key)

    async def shutdown(self) -> None:
        await self._agent.shutdown()


# ---------------------------------------------------------------------------
# Git observation
# ---------------------------------------------------------------------------


def _harness_visible_to_git() -> list[str]:
    """``git status --porcelain`` lines naming a path a harness run could write."""
    assert GIT is not None
    result = subprocess.run(
        [GIT, "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        line
        for line in result.stdout.splitlines()
        if any(prefix in line for prefix in _REPO_ARTIFACT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# The offline question — real filters, real agent, no provider
# ---------------------------------------------------------------------------


def _void_question() -> dict[str, Any]:
    """One dataset entry whose gold turn the live filters destroy.

    The damaged turn is in the FIRST session, which is what keeps this test free
    of provider calls. ``IngestDriver.ingest`` screens the chunks it is handed,
    and COMP-017 hands it one session at a time so each consolidation pass lands
    on a session boundary — so a question whose gold damage sits in a later
    session pays for every session before it. Putting the damage first is the
    only arrangement in which the void genuinely costs nothing; the second,
    clean session is here to be the one that is never reached.
    """
    return {
        "question_id": _OFFLINE_QUESTION_ID,
        "question_type": "temporal-reasoning",
        "question": "When did I become a certified PM?",
        "answer": "May 2023",
        "question_date": "2023/06/01 (Thu) 09:00",
        "answer_session_ids": [f"{_OFFLINE_QUESTION_ID}_s0"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/21 (Sun) 02:21"],
        "haystack_session_ids": [f"{_OFFLINE_QUESTION_ID}_s0", f"{_OFFLINE_QUESTION_ID}_s1"],
        "haystack_sessions": [
            [
                {
                    "role": "user",
                    "content": "I sat the PMP exam this morning.",
                    "has_answer": True,
                },
                {"role": "assistant", "content": INJECTION_TURN, "has_answer": True},
            ],
            [
                {
                    "role": "user",
                    "content": "I updated my CV with the new certification.",
                    "has_answer": False,
                }
            ],
        ],
    }


async def _run_offline_phase(results_dir: Path) -> tuple[PhaseReport, list[str]]:
    """Run the void question through the real phase runner at the real repo paths.

    Every collaborator is production except the preflight, which is COMP-020's
    own concern and needs a provider; the agent, the chunker, the fidelity gate,
    the ingest driver, the workspace lifecycle, the ledger, the budget governor
    and the manifest are all real. The judge builder is left production too —
    it raises without its key, so reaching it would fail this test loudly.
    """
    dataset = Dataset(questions=[_void_question()], sha256="0" * 64, revision="offline")
    estimate = estimate_run(
        dataset, profile=CallProfile(), pricing_table_version=CURRENT_PRICING_TABLE_VERSION
    )
    manifest = cli._build_manifest(
        phase="oracle",
        dataset=dataset,
        dataset_path=ORACLE_DATASET,
        estimate=estimate,
        strategy=None,
        pricing_version=CURRENT_PRICING_TABLE_VERSION,
    )
    results_path = results_dir / "oracle.jsonl"
    session_keys: list[str] = []

    async def build_agent(*, question_id: str, run_dir: Path) -> Any:
        return _CountingAgent(
            await build_eval_agent(question_id=question_id, run_dir=run_dir), session_keys
        )

    async def no_preflight(*, question_ids: Sequence[str]) -> None:
        """COMP-020 drives a real turn and a real distillation; it has its own tests."""

    RepoHygieneGuard(run_dir=RUNS_ROOT, results_path=results_path).check()
    with ResultLedger(results_path) as ledger:
        runner = PhaseRunner(
            dataset=dataset,
            manifest=manifest,
            ledger=ledger,
            budget=BudgetGovernor(
                estimate=estimate, ledger_path=results_path.with_suffix(".costs.jsonl")
            ),
            runs_root=RUNS_ROOT,
            results_dir=results_dir,
            preflight=no_preflight,
            agent_builder=build_agent,
        )
        report = await runner.run("oracle")
    return report, session_keys


# ---------------------------------------------------------------------------
# REQ-181 — the gold-evidence void, proven on the real filters and a real agent
# ---------------------------------------------------------------------------


def test_the_live_filters_destroy_the_gold_turn_this_smoke_feeds() -> None:
    """The fixture is filter-triggering against the REAL arcmemory filters.

    Asserted separately from the run below so that the day arcmemory stops
    deleting this prose, the failure says "the fixture no longer triggers the
    filter" rather than "the harness stopped voiding questions".
    """
    session = Session(
        conversation_id=f"{_OFFLINE_QUESTION_ID}_s1",
        source_date=date(2023, 5, 20),
        turns=[
            Turn(turn_id="t0", role="user", text="I sat the PMP exam this morning."),
            Turn(turn_id="t1", role="assistant", text=INJECTION_TURN),
        ],
    )
    (chunk,) = TurnChunker(max_event_chars=MAX_EVENT_CHARS).split(session, session_idx=1)

    verdict = SanitizeFidelityGate(max_event_chars=MAX_EVENT_CHARS).check(
        chunk, gold_turn_ids={chunk.turn_ids[1]}
    )

    assert verdict.gold_overlap, "the live filters no longer destroy this gold turn"
    assert verdict.shrunk_by > 0
    assert not verdict.ok


@requires_git
@pytest.mark.usefixtures("isolated_arc_home", "no_provider_keys")
async def test_a_filter_triggering_chunk_voids_the_question_before_any_spend(
    repo_results_dir: Path,
) -> None:
    """REQ-181: gold-evidence damage voids the question and is excluded from scoring.

    Driven through the real phase runner with a real ``ArcAgent``: the recorded
    session keys are empty because the gate screens the whole question before the
    driver feeds its first chunk, which is what makes the void free.
    """
    report, session_keys = await _run_offline_phase(repo_results_dir)

    assert (report.complete, report.void, report.error) == (0, 1, 0)
    assert session_keys == [], "a voided question must not cost a single agent turn"

    (row,) = [
        line
        for line in (repo_results_dir / "oracle.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    parsed = json.loads(row)
    assert parsed["status"] == "void"
    assert parsed["void_reason"] == "gold_evidence_filtered"
    assert parsed["verdict"] is None, "a void carries no score, so it cannot be averaged in"


# ---------------------------------------------------------------------------
# REQ-195/REQ-196 — the run leaves nothing git can see
# ---------------------------------------------------------------------------


@requires_git
@pytest.mark.usefixtures("isolated_arc_home", "no_provider_keys")
async def test_a_run_leaves_nothing_git_can_see(repo_results_dir: Path) -> None:
    """The real artifacts, at their real paths, are invisible to ``git status``.

    The assertion is a delta rather than an empty status: this repository is
    worked on while its tests run, so "clean" can only mean "this run added
    nothing", and that is exactly the property REQ-195/REQ-196 want. Nothing is
    cleaned up before the check — the results file, the cost ledger and
    ``run_manifest.json`` are all still on disk when git is asked.
    """
    before = _harness_visible_to_git()

    await _run_offline_phase(repo_results_dir)

    assert (repo_results_dir / "oracle.jsonl").is_file()
    assert (repo_results_dir / "run_manifest.json").is_file()
    assert (repo_results_dir / "oracle.costs.jsonl").is_file()
    assert _harness_visible_to_git() == before, (
        "the run put artifacts where git can see them; every path under "
        "evaluations/runs/ and evaluations/results/ must stay ignored (REQ-195)"
    )


# ---------------------------------------------------------------------------
# REQ-207 — three questions spanning types, against the real stack
# ---------------------------------------------------------------------------


@pytest.mark.slow
@requires_git
@requires_live_stack
@pytest.mark.usefixtures("isolated_arc_home")
async def test_three_questions_span_types_through_the_whole_real_stack(
    repo_results_dir: Path,
) -> None:
    """Ingest, consolidate, query and judge, on the real corpus and real providers.

    The Oracle corpus rather than S: it holds the same 500 questions over only
    their evidence sessions, so it walks the identical code path for a few dozen
    LLM calls instead of the several thousand three S haystacks would cost. The
    S-corpus smoke that unlocks ``--phase full-s`` is the CLI's ``--smoke N``;
    this is the same assertion at a price a test suite can pay.

    A consolidation pass per session is the load-bearing assertion (REQ-182):
    the pass is what turns captured events into the distilled memory the query
    step then has to retrieve from, and ``consolidate_poll_once()`` returning
    ``True`` is not evidence that one landed.
    """
    dataset = load_dataset(
        ORACLE_DATASET,
        # Self-pinned: the operator's pinned digest is not available to a test
        # run, and REQ-201's gate is proven against a fixture in
        # test_dataset_loader.py. What this run needs the loader for is the
        # parse and the provenance, not the pin.
        expected_sha256=None,
        revision=os.environ.get("LME_DATASET_REVISION", "unpinned-local"),
    )
    questions = cli.select_smoke_questions(dataset, count=SMOKE_QUESTIONS)
    question_ids = [str(raw["question_id"]) for raw in questions]
    assert len({str(raw["question_type"]) for raw in questions}) == SMOKE_QUESTIONS

    smoke_dataset = dataset.model_copy(update={"questions": questions})
    expected_passes = sum(
        len(list(LongMemEvalAdapter(dataset=smoke_dataset, question_id=qid).read()))
        for qid in question_ids
    )
    recorded: list[ConsolidationResult] = []
    results_path = repo_results_dir / "oracle.jsonl"

    estimate = estimate_run(
        smoke_dataset,
        profile=CallProfile(),
        pricing_table_version=CURRENT_PRICING_TABLE_VERSION,
    )
    RepoHygieneGuard(run_dir=RUNS_ROOT, results_path=results_path).check()
    before = _harness_visible_to_git()

    with ResultLedger(results_path) as ledger:
        runner = PhaseRunner(
            dataset=smoke_dataset,
            manifest=cli._build_manifest(
                phase="oracle",
                dataset=smoke_dataset,
                dataset_path=ORACLE_DATASET,
                estimate=estimate,
                strategy=None,
                pricing_version=CURRENT_PRICING_TABLE_VERSION,
            ),
            ledger=ledger,
            budget=BudgetGovernor(
                estimate=estimate, ledger_path=results_path.with_suffix(".costs.jsonl")
            ),
            runs_root=RUNS_ROOT,
            results_dir=repo_results_dir,
            preflight=cli._preflight_hook(
                dataset_path=ORACLE_DATASET,
                expected_sha256=dataset.sha256,
                revision=dataset.revision,
                runs_root=RUNS_ROOT,
                results_path=results_path,
            ),
            waiter_builder=_recording_waiter_builder(recorded),
        )
        report = await runner.run("oracle")

    # Mirrors the CLI's own smoke gate: anything short of every question
    # completing leaves the gate shut, so it fails here too.
    assert (report.complete, report.void, report.error) == (SMOKE_QUESTIONS, 0, 0)

    assert len(recorded) == expected_passes, (
        f"expected one consolidation pass per session ({expected_passes} across "
        f"{SMOKE_QUESTIONS} questions), got {len(recorded)}"
    )
    for result in recorded:
        assert result.fired
        assert result.last_run_before is None or result.last_run_after > result.last_run_before

    rows = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["question_id"] for row in rows] == question_ids
    for row in rows:
        assert row["status"] == "complete"
        assert row["answer"].strip(), "the agent's answer is recorded verbatim (REQ-187)"
        assert row["verdict"] is not None
        assert row["question_date_source"] in {"dataset", "derived"}

    assert _harness_visible_to_git() == before
