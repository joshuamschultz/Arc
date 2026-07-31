"""Tests for COMP-017 PhaseRunner — T-819.

Covers REQ-202 (strictly one question at a time, never more than one live agent)
and REQ-203 (three phases, each entered by an explicit flag; the ~50-question S
sample covers all six types and its stratification is recorded in the manifest).

*The sequencing is what is under test, so the sequencing is what is real.* The
ledger, the budget governor, the workspace lifecycle, the chunker, the fidelity
gate, the ingest driver, the judge's prompt selection and label parsing, the
scrubber, the adapter and the manifest are all the production classes. Only the
collaborators that need a network, a provider key or a live arcmemory runtime
binding are faked: the two agent builders, the consolidation waiter and the
query runner. Every fake yields to the event loop while it works, so a runner
that overlapped two questions — or built a second agent while one was live —
would be caught here rather than pass by never getting the chance to interleave.

Nothing here makes a network or LLM call, and nothing is written outside
``tmp_path``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from evaluations.ingest.lifecycle import INGEST_COMPLETE_MARKER
from evaluations.ingest.limits import MAX_EVENT_CHARS
from evaluations.ingest.types import Chunk
from evaluations.longmemeval.adapter import QuestionMeta
from evaluations.longmemeval.budget import (
    CURRENT_PRICING_TABLE_VERSION,
    BudgetGovernor,
    Estimate,
    SpendCeilingExceeded,
)
from evaluations.longmemeval.dataset import Dataset
from evaluations.longmemeval.ledger import ResultLedger, ResultRow
from evaluations.longmemeval.manifest import (
    MANIFEST_FILENAME,
    MeasurementScope,
    Provenance,
    RunManifest,
)
from evaluations.longmemeval.query import Answer
from evaluations.longmemeval.runner import (
    PHASES,
    PhaseNotSelectedError,
    PhaseRunner,
    StratificationError,
    resolve_phase,
    stratify_sample,
)
from evaluations.longmemeval.scoring import QUESTION_TYPES

# Ordinary benchmark prose the live filters destroy: it trips
# ``_INJECTION_RE``'s "you are now" branch, which deletes to end of line.
INJECTION_TURN = "Congratulations! You are now a certified PM as of May 2023."

# The natural LongMemEval-S distribution, as the PLAN's open question states it.
NATURAL_DISTRIBUTION = {
    "single-session-user": 70,
    "single-session-assistant": 56,
    "single-session-preference": 30,
    "multi-session": 133,
    "temporal-reasoning": 133,
    "knowledge-update": 78,
}


# ---------------------------------------------------------------------------
# Dataset construction
# ---------------------------------------------------------------------------


def _question(
    question_id: str,
    *,
    question_type: str = "single-session-user",
    sessions: Sequence[Sequence[tuple[str, bool]]] = ((("I passed the PMP exam.", True),),),
) -> dict[str, Any]:
    """One dataset entry, in the shape ``_RawQuestion`` validates."""
    session_ids = [f"{question_id}_s{index}" for index, _ in enumerate(sessions)]
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": "When did I pass the PMP exam?",
        "answer": "May 2023",
        "question_date": "2023/06/01 (Thu) 09:00",
        "answer_session_ids": session_ids[:1],
        "haystack_dates": [
            f"2023/05/{20 + index:02d} (Sat) 02:21" for index, _ in enumerate(sessions)
        ],
        "haystack_session_ids": session_ids,
        "haystack_sessions": [
            [
                {"role": "user", "content": text, "has_answer": gold}
                for text, gold in turns
            ]
            for turns in sessions
        ],
    }


def _dataset(*questions: dict[str, Any]) -> Dataset:
    return Dataset(questions=list(questions), sha256="0" * 64, revision="test")


def _natural_dataset() -> Dataset:
    """500 questions distributed exactly as LongMemEval-S is."""
    questions = [
        _question(f"{question_type}_{index}", question_type=question_type)
        for question_type, count in NATURAL_DISTRIBUTION.items()
        for index in range(count)
    ]
    return _dataset(*questions)


# ---------------------------------------------------------------------------
# Fakes for the seams that would otherwise need a network or a live runtime
# ---------------------------------------------------------------------------


class _Result:
    """What arcrun's ``RunResult`` gives the meter, and the judge its label."""

    def __init__(self, content: str = "May 2023", *, cost_usd: float = 0.01) -> None:
        self.content = content
        self.turns = 1
        self.cost_usd = cost_usd
        self.tokens_used = {"input": 100, "output": 20, "total": 120}


class _Registry:
    """A capability registry reporting the consolidation loop already gone."""

    async def unregister(self, kind: str, name: str) -> None:
        return None

    async def get_task(self, name: str) -> Any:
        return None


class Harness:
    """Shared record of everything the fakes did, in the order they did it.

    The event log is the evidence for REQ-202: an overlap between two agents, or
    between two questions, is visible as an interleaving here and nowhere else.
    """

    def __init__(self, *, judge_says: str = "yes", cost_usd: float = 0.01) -> None:
        self.events: list[str] = []
        self.live = 0
        self.max_live = 0
        self.building = 0
        self.max_building = 0
        self.judge_says = judge_says
        self.cost_usd = cost_usd
        self.asked: list[str] = []
        self.waits: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self.preflight_calls: list[list[str]] = []
        self.preflight_error: Exception | None = None

    async def _open(self, label: str) -> None:
        """Model construction as work that takes time, so an overlap can show."""
        self.building += 1
        self.max_building = max(self.max_building, self.building)
        self.events.append(f"build-start {label}")
        await asyncio.sleep(0)
        self.building -= 1
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        self.events.append(f"build-end {label}")

    def _close(self, label: str) -> None:
        self.live -= 1
        self.events.append(f"shutdown {label}")

    async def build_agent(self, *, question_id: str, run_dir: Path) -> Any:
        await self._open(f"agent:{question_id}")
        run_dir.mkdir(parents=True, exist_ok=True)
        return FakeAgent(self, f"agent:{question_id}", run_dir)

    async def build_judge(self, *, run_dir: Path) -> Any:
        await self._open(f"judge:{run_dir.name}")
        return FakeAgent(self, f"judge:{run_dir.name}", run_dir)

    def build_waiter(self, *, agent: Any, workspace: Path) -> FakeWaiter:
        return FakeWaiter(self, workspace)

    async def preflight(self, *, question_ids: Sequence[str]) -> None:
        self.preflight_calls.append(list(question_ids))
        if self.preflight_error is not None:
            raise self.preflight_error

    async def ask(self, agent: Any, meta: QuestionMeta, chunks: Sequence[Chunk]) -> Answer:
        """Drive one real query turn through the agent, then report the recall."""
        result = await agent.run_collected(meta.question, session_key="query")
        self.asked.append(meta.question)
        return Answer(
            text=result.content,
            question_date_used=meta.question_date,
            question_date_source=meta.question_date_source,
            recalled_chunk_ids=[f"{chunk.session_idx}:{chunk.chunk_idx}" for chunk in chunks[:1]],
        )


class FakeAgent:
    """An agent that refuses to be used after shutdown, or beside a second one."""

    def __init__(self, harness: Harness, label: str, run_dir: Path) -> None:
        self._harness = harness
        self._label = label
        self._capability_registry = _Registry()
        self._workspace = run_dir / "workspace"
        self._down = False

    async def run_collected(self, input_text: str, *, session_key: str) -> Any:
        assert not self._down, f"{self._label} ran a turn after shutdown"
        assert self._harness.live == 1, (
            f"{self._harness.live} agents were live during a turn on {self._label}"
        )
        await asyncio.sleep(0)
        self._harness.turns.append((self._label, session_key))
        content = self._harness.judge_says if self._label.startswith("judge:") else "May 2023"
        return _Result(content, cost_usd=self._harness.cost_usd)

    async def shutdown(self) -> None:
        self._down = True
        self._harness._close(self._label)


class FakeWaiter:
    """The consolidation waiter's shape, without an arcmemory runtime binding."""

    def __init__(self, harness: Harness, workspace: Path) -> None:
        self._harness = harness
        self._workspace = workspace
        self._entered = False

    @property
    def warnings(self) -> Sequence[str]:
        return ()

    def __enter__(self) -> FakeWaiter:
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._entered = False

    async def wait(self) -> Any:
        assert self._entered, "wait() outside the waiter's context manager"
        self._harness.waits.append(str(self._workspace))
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _provenance() -> Provenance:
    return Provenance(
        git_sha="deadbeef",
        git_dirty=False,
        harness_version="0.1.0",
        config_hash="c" * 64,
        dataset_sha256="0" * 64,
        dataset_revision="test",
        agent_model_id="anthropic/claude-sonnet-4-5-20250929",
        judge_model_id="openai/gpt-4o-2024-08-06",
        embedder_model_id="all-MiniLM-L6-v2",
        distiller_model_id="anthropic/claude-sonnet-4-5-20250929",
        tier="personal",
        run_timestamp_utc="2026-07-30T00:00:00+00:00",
    )


def _manifest() -> RunManifest:
    return RunManifest(
        provenance=_provenance(),
        measurement_scope=MeasurementScope(workpad_enabled=True, policy_enabled=True),
        pricing_table_version=CURRENT_PRICING_TABLE_VERSION,
    )


def _estimate(cost_usd: float = 100.0) -> Estimate:
    return Estimate(
        n_calls=10,
        tokens_in=1000,
        tokens_out=100,
        cost_usd=cost_usd,
        pricing_table_version=CURRENT_PRICING_TABLE_VERSION,
        n_questions=1,
        n_sessions=1,
        n_chunks=1,
        n_voided_questions=0,
    )


def _runner(
    tmp_path: Path,
    dataset: Dataset,
    harness: Harness,
    *,
    ledger: ResultLedger | None = None,
    estimate_usd: float = 100.0,
    keep_workspace_on_failure: bool = False,
    sample_size: int = 12,
) -> PhaseRunner:
    return PhaseRunner(
        dataset=dataset,
        manifest=_manifest(),
        ledger=ledger or ResultLedger(tmp_path / "results" / "rows.jsonl"),
        budget=BudgetGovernor(
            estimate=_estimate(estimate_usd), ledger_path=tmp_path / "results" / "spend.jsonl"
        ),
        runs_root=tmp_path / "runs",
        results_dir=tmp_path / "results",
        preflight=harness.preflight,
        agent_builder=harness.build_agent,
        judge_builder=harness.build_judge,
        waiter_builder=harness.build_waiter,
        asker=harness,
        keep_workspace_on_failure=keep_workspace_on_failure,
        sample_size=sample_size,
    )


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    text = (tmp_path / "results" / "rows.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# REQ-203 — three phases, each entered by an explicit flag
# ---------------------------------------------------------------------------


def test_a_phase_is_never_chosen_for_the_operator() -> None:
    """Nothing defaults to a phase: the three differ by orders of magnitude in spend."""
    with pytest.raises(PhaseNotSelectedError, match="explicitly"):
        resolve_phase(None)
    with pytest.raises(PhaseNotSelectedError, match="full"):
        resolve_phase("everything")
    assert [resolve_phase(name) for name in PHASES] == list(PHASES)


def test_run_takes_the_phase_as_a_required_argument() -> None:
    """A default here would let ``full-s`` start by omission."""
    parameter = inspect.signature(PhaseRunner.run).parameters["phase"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


async def test_only_the_named_phase_runs(tmp_path: Path) -> None:
    """One invocation is one phase; the runner never advances into the next."""
    dataset = _dataset(_question("q1"), _question("q2"))
    harness = Harness()

    report = await _runner(tmp_path, dataset, harness).run("oracle")

    assert report.phase == "oracle"
    assert report.requested == 2
    assert report.complete == 2
    assert report.stratification is None
    assert len(harness.preflight_calls) == 1, "preflight runs once per phase, not per question"
    assert harness.preflight_calls[0] == ["q1", "q2"]


async def test_a_full_phase_runs_every_question_in_dataset_order(tmp_path: Path) -> None:
    dataset = _dataset(_question("q1"), _question("q2"), _question("q3"))
    harness = Harness()

    report = await _runner(tmp_path, dataset, harness).run("full-s")

    assert report.requested == 3
    assert [row["question_id"] for row in _rows(tmp_path)] == ["q1", "q2", "q3"]


async def test_a_stratification_strategy_is_refused_on_an_ungated_phase(tmp_path: Path) -> None:
    """Oracle and full-S run everything; a strategy passed there would be ignored."""
    harness = Harness()
    runner = _runner(tmp_path, _dataset(_question("q1")), harness)

    with pytest.raises(StratificationError, match="every question"):
        await runner.run("oracle", strategy="even")


# ---------------------------------------------------------------------------
# REQ-203 — the sampled-S stratification
# ---------------------------------------------------------------------------


async def test_sample_s_refuses_to_pick_a_stratification_strategy(tmp_path: Path) -> None:
    """The even-vs-proportional call is an OPEN question, not a default."""
    harness = Harness()
    runner = _runner(tmp_path, _natural_dataset(), harness)

    with pytest.raises(StratificationError, match="open question"):
        await runner.run("sample-s")

    assert harness.events == [], "nothing may be built before the strategy is named"


def test_even_stratification_covers_all_six_types() -> None:
    sample = stratify_sample(_natural_dataset(), strategy="even")

    assert sorted(sample.per_type) == sorted(QUESTION_TYPES)
    assert sum(sample.per_type.values()) == 50
    assert min(sample.per_type.values()) >= 8
    assert len(sample.question_ids) == 50
    assert len(set(sample.question_ids)) == 50


def test_proportional_stratification_matches_the_natural_distribution() -> None:
    """The PLAN's own worked example: proportional puts preference at three."""
    sample = stratify_sample(_natural_dataset(), strategy="proportional")

    assert sum(sample.per_type.values()) == 50
    assert sample.per_type["single-session-preference"] == 3
    assert sample.per_type["multi-session"] == 13
    assert sample.per_type["temporal-reasoning"] == 13
    assert min(sample.per_type.values()) >= 1, "every type must be covered"


def test_the_two_strategies_draw_different_samples() -> None:
    """If they agreed, recording which one was used would be pointless."""
    even = stratify_sample(_natural_dataset(), strategy="even")
    proportional = stratify_sample(_natural_dataset(), strategy="proportional")

    assert even.per_type != proportional.per_type
    assert even.strategy == "even"
    assert proportional.strategy == "proportional"


def test_a_missing_question_type_refuses_to_produce_a_sample() -> None:
    dataset = _dataset(*[_question(f"q{index}") for index in range(10)])

    with pytest.raises(StratificationError, match="no questions of type"):
        stratify_sample(dataset, strategy="even", size=6)


def test_a_sample_smaller_than_the_six_types_is_refused() -> None:
    with pytest.raises(StratificationError, match="cannot cover"):
        stratify_sample(_natural_dataset(), strategy="even", size=5)


def test_a_scarce_type_is_still_covered(tmp_path: Path) -> None:
    """Proportional rounding would zero a type this small; the fit refuses to."""
    scarce = {**NATURAL_DISTRIBUTION, "single-session-preference": 1}
    questions = [
        _question(f"{question_type}_{index}", question_type=question_type)
        for question_type, count in scarce.items()
        for index in range(count)
    ]
    sample = stratify_sample(_dataset(*questions), strategy="proportional", size=50)

    assert sample.per_type["single-session-preference"] == 1
    assert sum(sample.per_type.values()) == 50


async def test_the_stratification_is_recorded_in_the_manifest(tmp_path: Path) -> None:
    """Without this the sample is unreproducible and two runs are incomparable."""
    harness = Harness()
    runner = _runner(tmp_path, _natural_dataset(), harness, sample_size=12)

    report = await runner.run("sample-s", strategy="proportional")

    assert report.stratification is not None
    manifest = json.loads((tmp_path / "results" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    recorded = manifest["sample_stratification"]
    assert recorded["strategy"] == "proportional"
    assert recorded["size"] == 12
    assert sorted(recorded["per_type"]) == sorted(QUESTION_TYPES)
    assert recorded["question_ids"] == report.stratification.question_ids
    assert manifest["provenance"]["git_sha"] == "deadbeef", "COMP-016's block survives the merge"


async def test_an_ungated_phase_records_no_stratification(tmp_path: Path) -> None:
    harness = Harness()
    await _runner(tmp_path, _dataset(_question("q1")), harness).run("oracle")

    manifest = json.loads((tmp_path / "results" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["sample_stratification"] is None


# ---------------------------------------------------------------------------
# REQ-202 — one question, one agent, at a time
# ---------------------------------------------------------------------------


async def test_never_more_than_one_agent_is_live(tmp_path: Path) -> None:
    """The whole simplification rests on this: no shared WORM chain, no arcstore."""
    dataset = _dataset(_question("q1"), _question("q2"), _question("q3"))
    harness = Harness()

    await _runner(tmp_path, dataset, harness).run("oracle")

    assert harness.max_live == 1, f"agents overlapped: {harness.events}"
    assert harness.live == 0, "an agent outlived its question"


async def test_agent_construction_is_never_concurrent(tmp_path: Path) -> None:
    """Two builds in flight would mean two questions in flight."""
    dataset = _dataset(_question("q1"), _question("q2"), _question("q3"))
    harness = Harness()

    await _runner(tmp_path, dataset, harness).run("oracle")

    assert harness.max_building == 1, f"two builds overlapped: {harness.events}"

    # Every build ends before the next one starts, so the log alternates.
    builds = [event for event in harness.events if event.startswith("build-")]
    assert builds[::2] == [event for event in builds if event.startswith("build-start")]
    assert builds[1::2] == [event for event in builds if event.startswith("build-end")]


async def test_the_judge_is_built_only_after_the_eval_agent_is_down(tmp_path: Path) -> None:
    """The strongest form of REQ-202: even the grader never coexists with the SUT."""
    harness = Harness()

    await _runner(tmp_path, _dataset(_question("q1")), harness).run("oracle")

    assert harness.events == [
        "build-start agent:q1",
        "build-end agent:q1",
        "shutdown agent:q1",
        "build-start judge:q1",
        "build-end judge:q1",
        "shutdown judge:q1",
    ]


async def test_questions_are_processed_strictly_one_at_a_time(tmp_path: Path) -> None:
    """Each question's whole lifecycle closes before the next one opens."""
    dataset = _dataset(_question("q1"), _question("q2"))
    harness = Harness()

    await _runner(tmp_path, dataset, harness).run("oracle")

    first = harness.events.index("build-start agent:q2")
    assert harness.events.index("shutdown judge:q1") < first
    assert [label for label, _ in harness.turns] == [
        "agent:q1",
        "agent:q1",
        "judge:q1",
        "agent:q2",
        "agent:q2",
        "judge:q2",
    ]


def test_the_loop_holds_no_concurrency_machinery() -> None:
    """There is no semaphore, no gather and no backpressure in this harness.

    A source assertion because the absence is the design: adding any of them
    silently reintroduces the shared WORM chain, the shared arcstore and the
    rate-limit cascade this component exists to keep out of scope.
    """
    from evaluations.longmemeval import runner as runner_module

    source = Path(runner_module.__file__).read_text(encoding="utf-8")
    for machinery in ("gather(", "Semaphore(", "create_task(", "TaskGroup(", "as_completed("):
        assert machinery not in source, f"{machinery} appeared in the sequential runner"


# ---------------------------------------------------------------------------
# The per-question loop: ingest, consolidate, ask, judge, record, tear down
# ---------------------------------------------------------------------------


async def test_a_completed_question_streams_one_terminal_row(tmp_path: Path) -> None:
    harness = Harness()

    await _runner(tmp_path, _dataset(_question("q1")), harness).run("oracle")

    (row,) = _rows(tmp_path)
    assert row["status"] == "complete"
    assert row["question_id"] == "q1"
    assert row["question_type"] == "single-session-user"
    assert row["answer"] == "May 2023"
    assert row["verdict"]["correct"] is True
    assert row["verdict"]["judge_model_id"] == "gpt-4o-2024-08-06"
    assert row["question_date"] == "2023-06-01"
    assert row["question_date_source"] == "dataset"
    assert row["provenance"]["question_id"] == "q1"
    assert row["provenance"]["session_ingest_order"] == ["q1_s0"]
    assert row["retrieval"]["gold_turn_ids"] == ["q1_s0:0"]
    assert row["retrieval"]["gold_session_ids"] == ["q1_s0"]
    assert row["retrieval"]["retrieved_turn_ids"] == ["q1_s0:0"]


async def test_a_wrong_answer_is_recorded_as_a_failed_verdict(tmp_path: Path) -> None:
    harness = Harness(judge_says="no")

    await _runner(tmp_path, _dataset(_question("q1")), harness).run("oracle")

    (row,) = _rows(tmp_path)
    assert row["status"] == "complete"
    assert row["verdict"]["correct"] is False


async def test_an_abstention_question_carries_no_retrieval(tmp_path: Path) -> None:
    """``_abs`` names no answer session, so recall against it is meaningless."""
    harness = Harness()

    await _runner(tmp_path, _dataset(_question("q1_abs")), harness).run("oracle")

    (row,) = _rows(tmp_path)
    assert row["is_abstention"] is True
    assert row["retrieval"] is None


async def test_a_consolidation_pass_fires_at_every_session_boundary(tmp_path: Path) -> None:
    harness = Harness()
    question = _question(
        "q1",
        sessions=(
            (("I passed the PMP exam.", True),),
            (("I started a new job.", False),),
            (("I moved to Denver.", False),),
        ),
    )

    await _runner(tmp_path, _dataset(question), harness).run("oracle")

    assert len(harness.waits) == 3, "one driven pass per session boundary (COMP-008)"


async def test_the_workspace_is_marked_and_then_torn_down(tmp_path: Path) -> None:
    harness = Harness()

    await _runner(tmp_path, _dataset(_question("q1")), harness).run("oracle")

    assert not (tmp_path / "runs" / "q1").exists(), "teardown did not run"


async def test_teardown_runs_in_a_finally_when_the_question_raises(tmp_path: Path) -> None:
    harness = Harness()
    runner = _runner(tmp_path, _dataset(_question("q1"), _question("q2")), harness)

    async def exploding_judge(*, run_dir: Path) -> Any:
        raise RuntimeError("the provider rate-limited the judge")

    runner._judge_builder = exploding_judge

    report = await runner.run("oracle")

    assert report.error == 2
    assert not (tmp_path / "runs" / "q1").exists()
    assert not (tmp_path / "runs" / "q2").exists()
    assert [row["status"] for row in _rows(tmp_path)] == ["error", "error"]


async def test_a_retained_workspace_survives_under_keep_on_failure(tmp_path: Path) -> None:
    harness = Harness()
    runner = _runner(
        tmp_path, _dataset(_question("q1")), harness, keep_workspace_on_failure=True
    )

    async def exploding_judge(*, run_dir: Path) -> Any:
        raise RuntimeError("the provider rate-limited the judge")

    runner._judge_builder = exploding_judge
    await runner.run("oracle")

    assert (tmp_path / "runs" / "q1").is_dir(), "REQ-213 retains an errored workspace"


async def test_an_errored_question_does_not_stop_the_phase(tmp_path: Path) -> None:
    """A rate limit marks the row error so resume rebuilds it; it never aborts."""
    harness = Harness()
    runner = _runner(tmp_path, _dataset(_question("q1"), _question("q2")), harness)
    calls = {"n": 0}
    real_builder = harness.build_judge

    async def flaky_judge(*, run_dir: Path) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 from the provider")
        return await real_builder(run_dir=run_dir)

    runner._judge_builder = flaky_judge
    report = await runner.run("oracle")

    assert (report.error, report.complete) == (1, 1)
    assert [row["status"] for row in _rows(tmp_path)] == ["error", "complete"]


# ---------------------------------------------------------------------------
# Voids, resume and the phase-level aborts
# ---------------------------------------------------------------------------


async def test_gold_evidence_damage_voids_the_question(tmp_path: Path) -> None:
    harness = Harness()
    question = _question("q1", sessions=(((INJECTION_TURN, True),),))

    report = await _runner(tmp_path, _dataset(question), harness).run("oracle")

    assert report.void == 1
    (row,) = _rows(tmp_path)
    assert row["status"] == "void"
    assert row["void_reason"] == "gold_evidence_filtered"
    assert row["verdict"] is None
    assert harness.asked == [], "a voided question is never asked"


async def test_a_turn_over_the_cap_voids_before_an_agent_is_built(tmp_path: Path) -> None:
    """REQ-177: the question is voided rather than its evidence truncated."""
    harness = Harness()
    question = _question("q1", sessions=((("x" * (MAX_EVENT_CHARS + 1), True),),))

    report = await _runner(tmp_path, _dataset(question), harness).run("oracle")

    assert report.void == 1
    (row,) = _rows(tmp_path)
    assert row["void_reason"] == "turn_exceeds_cap"
    assert row["cost"]["cost_usd"] == 0.0
    assert harness.events == [], "a question voided by chunking costs nothing"


async def test_a_question_already_complete_is_skipped(tmp_path: Path) -> None:
    """REQ-204: the question is the atomic unit of resume."""
    ledger = ResultLedger(tmp_path / "results" / "rows.jsonl")
    ledger.append(
        ResultRow(
            question_id="q1",
            status="complete",
            question_type="single-session-user",
            is_abstention=False,
            provenance={"git_sha": "deadbeef"},
        )
    )
    harness = Harness()
    runner = _runner(
        tmp_path, _dataset(_question("q1"), _question("q2")), harness, ledger=ledger
    )

    report = await runner.run("oracle")

    assert (report.skipped, report.complete) == (1, 1)
    assert [label for label, _ in harness.turns if label.startswith("agent:")] == [
        "agent:q2",
        "agent:q2",
    ]


async def test_a_marked_workspace_is_resumed_without_re_ingesting(tmp_path: Path) -> None:
    """REQ-205: the marker means the haystack is whole, so ingest is not paid twice."""
    run_dir = (tmp_path / "runs" / "q1").resolve()
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / INGEST_COMPLETE_MARKER).touch()
    harness = Harness()

    await _runner(tmp_path, _dataset(_question("q1")), harness).run("oracle")

    assert harness.waits == [], "a resumed workspace re-runs no consolidation pass"
    assert [key for label, key in harness.turns if label == "agent:q1"] == ["query"], (
        "a resumed workspace re-feeds no chunk"
    )


async def test_a_preflight_failure_aborts_before_any_question(tmp_path: Path) -> None:
    harness = Harness()
    harness.preflight_error = RuntimeError("the distiller is None after startup()")
    runner = _runner(tmp_path, _dataset(_question("q1")), harness)

    with pytest.raises(RuntimeError, match="distiller"):
        await runner.run("oracle")

    assert harness.events == []
    assert not (tmp_path / "results" / MANIFEST_FILENAME).exists()


async def test_the_spend_ceiling_aborts_the_phase(tmp_path: Path) -> None:
    """REQ-209: the ceiling aborts, and the question that broke it is on disk."""
    harness = Harness(cost_usd=1.0)
    runner = _runner(
        tmp_path,
        _dataset(_question("q1"), _question("q2")),
        harness,
        estimate_usd=0.5,
    )

    with pytest.raises(SpendCeilingExceeded):
        await runner.run("oracle")

    assert [row["question_id"] for row in _rows(tmp_path)] == ["q1"]
    assert harness.asked == ["When did I pass the PMP exam?"], "q2 never started"


async def test_measured_cost_is_recorded_per_question(tmp_path: Path) -> None:
    """REQ-212: real provider usage, so a median-cost diff catches a regression."""
    harness = Harness(cost_usd=0.25)

    await _runner(tmp_path, _dataset(_question("q1")), harness).run("oracle")

    (row,) = _rows(tmp_path)
    # One ingest chunk, one query turn, one judge turn.
    assert row["cost"]["n_llm_calls"] == 3
    assert row["cost"]["cost_usd"] == pytest.approx(0.75)
    assert row["cost"]["tokens_in"] == 300
    assert row["cost"]["tokens_out"] == 60
    assert row["cost"]["wall_seconds"] >= 0.0
