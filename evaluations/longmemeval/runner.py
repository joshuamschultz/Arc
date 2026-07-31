"""PhaseRunner (COMP-017 / REQ-202, REQ-203) — the strictly sequential outer loop.

One question at a time, one live ``ArcAgent`` at a time. That single decision is
what keeps the flocked WORM audit chain, the shared ``~/.arc/store`` SQLite,
provider rate-limit cascades and ``contextvars`` sibling-task hazards out of this
harness entirely — so there is no semaphore, no ``asyncio.gather`` and no
backpressure logic anywhere below, and a test asserts the source stays that way.
The invariant is enforced in the strongest available form: the eval agent is shut
down before the judge agent is built, so even the judge never coexists with the
system under test.

*Three phases, each entered by naming it.* ``oracle`` proves the whole path
across 500 near-free questions, ``sample-s`` measures a stratified ~50, ``full-s``
spends the ~40,000 ingest calls. Nothing here advances from one to the next:
:func:`resolve_phase` refuses to pick a phase, and :meth:`PhaseRunner.run` takes
the phase as a required argument, so entering a later phase is always an operator
flag (REQ-203).

*The sample-S stratification is an OPEN decision, deliberately unresolved here.*
Whether the six types are weighted evenly or in proportion to their natural
distribution is deferred until Oracle results show which types are weakest, so
``strategy`` has no default, ``sample-s`` refuses to run without it, and the
choice that was made is written into the run manifest beside the counts it
produced.

*Every collaborator needing a network, a provider key or a live arcmemory runtime
binding is injected*, with the production wiring as its default. That is what
lets the loop's own sequencing — one agent, one question, teardown in a
``finally`` — be proven without an LLM call.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from evaluations.longmemeval.ingest.agent_factory import build_eval_agent
from evaluations.longmemeval.ingest.chunker import TurnChunker, TurnExceedsCapError
from evaluations.longmemeval.ingest.consolidation import ConsolidationWaiter
from evaluations.longmemeval.ingest.driver import IngestDriver
from evaluations.longmemeval.ingest.fidelity import GoldEvidenceFilteredError, SanitizeFidelityGate
from evaluations.longmemeval.ingest.lifecycle import WorkspaceLifecycle, assert_leftovers_under_threshold
from evaluations.longmemeval.ingest.types import Chunk
from evaluations.longmemeval.adapter import LongMemEvalAdapter, QuestionMeta
from evaluations.longmemeval.budget import MAX_EVENT_CHARS, BudgetGovernor, QuestionCost
from evaluations.longmemeval.dataset import Dataset
from evaluations.longmemeval.judge import (
    JudgeAgent,
    Verdict,
    build_judge_agent,
    is_abstention_question,
)
from evaluations.longmemeval.ledger import ResultLedger, ResultRow
from evaluations.longmemeval.manifest import MANIFEST_FILENAME, RunManifest
from evaluations.longmemeval.query import Answer, QueryRunner
from evaluations.longmemeval.scoring import QUESTION_TYPES, UnknownQuestionTypeError
from evaluations.longmemeval.scrub import ArtifactScrubber

_LOG = logging.getLogger(__name__)

Phase = Literal["oracle", "sample-s", "full-s"]

PHASES: tuple[Phase, ...] = ("oracle", "sample-s", "full-s")
"""The three phases in the order an operator is expected to walk them.

Order is documentation, not a state machine: this module never advances from one
phase to the next, so entering a later phase always costs an explicit flag.
"""

StratificationStrategy = Literal["even", "proportional"]

SAMPLE_S_SIZE = 50
"""The ~50-question S sample (REQ-203). Every one of the six types is covered."""

VoidReason = Literal["gold_evidence_filtered", "turn_exceeds_cap"]

_VOID_REASONS: Mapping[type[Exception], VoidReason] = {
    GoldEvidenceFilteredError: "gold_evidence_filtered",
    TurnExceedsCapError: "turn_exceeds_cap",
}
"""The two failures that void a question instead of erroring it.

Both mean the harness could not put the question's own evidence in front of the
agent, so scoring the answer would charge arcmemory for the harness's damage.
"""


class PhaseNotSelectedError(RuntimeError):
    """No phase was named, so there is nothing to run (REQ-203).

    Never defaulted: the three phases differ by roughly two orders of magnitude
    in spend, and a default would let ``full-s`` start by omission.
    """


class StratificationError(RuntimeError):
    """The sample-S question set could not be built as specified."""


class SampleStratification(BaseModel):
    """How the ~50-question S sample was drawn, as recorded in the run manifest.

    ``question_ids`` is written out in full rather than left derivable: the
    strategy plus the counts do not name the questions, and two runs are only
    comparable if they measured the same ones.
    """

    model_config = ConfigDict(frozen=True)

    strategy: StratificationStrategy
    size: int
    per_type: dict[str, int]
    question_ids: list[str]


class PhaseReport(BaseModel):
    """What one phase did. Rows are the record; this is the tally over them."""

    model_config = ConfigDict(frozen=True)

    phase: Phase
    requested: int
    skipped: int
    complete: int
    void: int
    error: int
    stratification: SampleStratification | None
    wall_seconds: float


# ---------------------------------------------------------------------------
# The injected seams
# ---------------------------------------------------------------------------


class HarnessAgent(Protocol):
    """The slice of ``ArcAgent`` the runner drives.

    ``_capability_registry`` and ``_workspace`` are private on ``ArcAgent`` and
    have no public accessors; the ingest driver needs the first to prove the
    background consolidation loop is stopped, and the consolidation waiter needs
    the second to read the markers a pass leaves behind.
    """

    _capability_registry: Any
    _workspace: Path

    async def run_collected(self, input_text: str, *, session_key: str) -> Any: ...

    async def shutdown(self) -> None: ...


class AgentBuilder(Protocol):
    """Builds one question's throwaway agent (COMP-006)."""

    async def __call__(self, *, question_id: str, run_dir: Path) -> HarnessAgent: ...


class JudgeBuilder(Protocol):
    """Builds the grading agent under the question's run dir (COMP-010)."""

    async def __call__(self, *, run_dir: Path) -> HarnessAgent: ...


class ConsolidationSession(Protocol):
    """The consolidation waiter as the loop uses it (COMP-008)."""

    @property
    def warnings(self) -> Sequence[str]: ...

    def __enter__(self) -> ConsolidationSession: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def wait(self) -> Any: ...


class WaiterBuilder(Protocol):
    """Constructs a consolidation waiter for one question's agent.

    ``agent`` is typed ``Any`` because the waiter's own contract with the agent
    belongs to COMP-008, not here.
    """

    def __call__(self, *, agent: Any, workspace: Path) -> ConsolidationSession: ...


class QuestionAsker(Protocol):
    """Puts the benchmark question to the agent that ingested it (COMP-009)."""

    async def ask(self, agent: Any, meta: QuestionMeta, chunks: Sequence[Chunk]) -> Answer: ...


class PreflightHook(Protocol):
    """Runs COMP-020's gates once per phase, before any question is ingested."""

    async def __call__(self, *, question_ids: Sequence[str]) -> None: ...


# ---------------------------------------------------------------------------
# Phase and sample selection
# ---------------------------------------------------------------------------


def resolve_phase(name: str | None) -> Phase:
    """Turn an operator's flag into a phase, refusing to choose one for them."""
    for phase in PHASES:
        if phase == name:
            return phase
    raise PhaseNotSelectedError(
        f"no phase named {name!r}; name one of {list(PHASES)} explicitly. The phases "
        "differ by orders of magnitude in spend, so none of them is a default (REQ-203)"
    )


def stratify_sample(
    dataset: Dataset,
    *,
    strategy: StratificationStrategy,
    size: int = SAMPLE_S_SIZE,
) -> SampleStratification:
    """Draw the S sample so that all six question types are covered (REQ-203).

    ``strategy`` is an open decision this function refuses to make: ``even``
    weighs the six types equally, ``proportional`` matches their natural
    distribution (which puts ``single-session-preference`` at three). Which one
    is right depends on Oracle results that do not exist yet, so the caller names
    it and the choice is recorded.

    Selection within a type is the dataset's own order, so a sample is
    reproducible from the strategy alone — and the ids are written out anyway.
    """
    available = _questions_by_type(dataset)
    missing = [name for name in QUESTION_TYPES if not available[name]]
    if missing:
        raise StratificationError(
            f"the dataset holds no questions of type {missing}; a sample that misses a "
            "type cannot support the macro mean the benchmark reports"
        )
    if size < len(QUESTION_TYPES):
        raise StratificationError(
            f"a sample of {size} cannot cover all {len(QUESTION_TYPES)} question types"
        )
    capacity = {name: len(ids) for name, ids in available.items()}
    if sum(capacity.values()) < size:
        raise StratificationError(
            f"the dataset holds {sum(capacity.values())} questions, fewer than the "
            f"requested sample of {size}"
        )

    shares = _even_shares(size) if strategy == "even" else _proportional_shares(size, capacity)
    per_type = _fit(shares, capacity, size)
    return SampleStratification(
        strategy=strategy,
        size=size,
        per_type=per_type,
        question_ids=[
            question_id
            for name in QUESTION_TYPES
            for question_id in available[name][: per_type[name]]
        ],
    )


def _questions_by_type(dataset: Dataset) -> dict[str, list[str]]:
    """Question ids grouped by type, each group in the dataset's own order."""
    by_type: dict[str, list[str]] = {name: [] for name in QUESTION_TYPES}
    for raw in dataset.questions:
        question_type = str(raw["question_type"])
        if question_type not in by_type:
            raise UnknownQuestionTypeError(
                f"{question_type!r} is not one of the six LongMemEval question types"
            )
        by_type[question_type].append(str(raw["question_id"]))
    return by_type


def _even_shares(size: int) -> dict[str, int]:
    """Equal shares, with the remainder going to the types in canonical order."""
    base, extra = divmod(size, len(QUESTION_TYPES))
    return {name: base + (1 if i < extra else 0) for i, name in enumerate(QUESTION_TYPES)}


def _proportional_shares(size: int, capacity: Mapping[str, int]) -> dict[str, int]:
    """Shares matching the natural distribution, by the largest-remainder method.

    Plain rounding would not sum to ``size``; largest remainder does, and it puts
    the questions it rounds up where the fractional part was biggest rather than
    wherever the iteration happened to end.
    """
    total = sum(capacity.values())
    quotas = {name: size * capacity[name] / total for name in QUESTION_TYPES}
    shares = {name: int(quotas[name]) for name in QUESTION_TYPES}
    ranked = sorted(
        QUESTION_TYPES,
        key=lambda name: (-(quotas[name] - shares[name]), QUESTION_TYPES.index(name)),
    )
    for name in ranked[: size - sum(shares.values())]:
        shares[name] += 1
    return shares


def _fit(shares: Mapping[str, int], capacity: Mapping[str, int], size: int) -> dict[str, int]:
    """Clamp shares to what exists, guarantee every type, and land on ``size``.

    Both strategies can ask for more of a type than the dataset holds, and both
    can round one to zero. Neither is acceptable — a zero drops a type out of the
    macro mean — so shares are clamped and the difference is moved: taken from
    the largest stratum, given to the least-filled one.
    """
    fitted = {name: min(max(shares[name], 1), capacity[name]) for name in QUESTION_TYPES}
    for _ in range(size + len(QUESTION_TYPES)):
        total = sum(fitted.values())
        if total == size:
            return fitted
        if total > size:
            donors = [name for name in QUESTION_TYPES if fitted[name] > 1]
            if not donors:
                break
            fitted[max(donors, key=lambda name: fitted[name])] -= 1
        else:
            takers = [name for name in QUESTION_TYPES if fitted[name] < capacity[name]]
            if not takers:
                break
            fitted[min(takers, key=lambda name: fitted[name] / capacity[name])] += 1
    raise StratificationError(
        f"could not fit a {size}-question sample covering all six types into "
        f"{dict(capacity)}"
    )


def write_run_manifest(
    manifest: RunManifest,
    *,
    stratification: SampleStratification | None,
    out_dir: Path,
) -> Path:
    """Write ``run_manifest.json`` with the sample stratification beside it.

    ``RunManifest`` (COMP-016) carries no stratification field, so the record is
    merged in at write time rather than by forking that model into a second
    definition of the same block.
    """
    payload = manifest.model_dump(mode="json")
    payload["sample_stratification"] = (
        None if stratification is None else stratification.model_dump(mode="json")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / MANIFEST_FILENAME
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Cost metering
# ---------------------------------------------------------------------------


class _Meter:
    """Accumulates one question's real provider usage off every finished turn.

    Read from arcrun's ``RunResult`` rather than estimated, so the ceiling
    (REQ-209) enforces against measured spend. Consolidation passes are the known
    blind spot: they call the distiller inside arcmemory and return no
    ``RunResult``, so their tokens land in the provider's bill and not here.
    """

    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.n_llm_calls = 0

    def record(self, result: Any) -> None:
        """Fold one finished turn into the running total."""
        tokens = result.tokens_used
        self.tokens_in += int(tokens.get("input", 0))
        self.tokens_out += int(tokens.get("output", 0))
        self.cost_usd += float(result.cost_usd)
        self.n_llm_calls += int(result.turns)

    def cost(self, question_id: str, *, wall_seconds: float) -> QuestionCost:
        """The five fields REQ-212 records per question."""
        return QuestionCost(
            question_id=question_id,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            cost_usd=self.cost_usd,
            n_llm_calls=self.n_llm_calls,
            wall_seconds=wall_seconds,
        )


class _MeteredAgent:
    """An agent that reports what its turns cost, and is otherwise transparent.

    Wrapping is what makes the ingest half of a question's spend visible: the
    driver owns the feed loop and discards its ``RunResult``s, so the only place
    left to observe them is the call itself.
    """

    def __init__(self, agent: Any, meter: _Meter) -> None:
        self._agent = agent
        self._meter = meter
        self._capability_registry = agent._capability_registry

    async def run_collected(self, input_text: str, *, session_key: str) -> Any:
        result = await self._agent.run_collected(input_text, session_key=session_key)
        self._meter.record(result)
        return result


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class PhaseRunner:
    """Runs one gated phase, one question at a time (COMP-017)."""

    def __init__(
        self,
        *,
        dataset: Dataset,
        manifest: RunManifest,
        ledger: ResultLedger,
        budget: BudgetGovernor,
        runs_root: Path,
        results_dir: Path,
        preflight: PreflightHook,
        agent_builder: AgentBuilder = build_eval_agent,
        judge_builder: JudgeBuilder = build_judge_agent,
        waiter_builder: WaiterBuilder = ConsolidationWaiter,
        asker: QuestionAsker | None = None,
        keep_workspace_on_failure: bool = False,
        max_event_chars: int = MAX_EVENT_CHARS,
        sample_size: int = SAMPLE_S_SIZE,
    ) -> None:
        self._dataset = dataset
        self._manifest = manifest
        self._ledger = ledger
        self._budget = budget
        self._runs_root = runs_root
        self._results_dir = results_dir
        self._preflight = preflight
        self._agent_builder = agent_builder
        self._judge_builder = judge_builder
        self._waiter_builder = waiter_builder
        self._asker: QuestionAsker = QueryRunner() if asker is None else asker
        self._keep_on_failure = keep_workspace_on_failure
        self._sample_size = sample_size
        self._chunker = TurnChunker(max_event_chars=max_event_chars)
        self._driver = IngestDriver(gate=SanitizeFidelityGate(max_event_chars=max_event_chars))
        self._scrubber = ArtifactScrubber()

    async def run(
        self,
        phase: Phase,
        *,
        strategy: StratificationStrategy | None = None,
    ) -> PhaseReport:
        """Run every question of ``phase``, in order, one at a time.

        ``phase`` is positional and required, and nothing here starts another
        one: a later phase is always a separate invocation behind its own flag
        (REQ-203). ``SpendCeilingExceeded`` and ``PreflightError`` propagate —
        both are phase-level aborts, not per-question outcomes.
        """
        started = time.monotonic()
        question_ids, stratification = self._select(phase, strategy)

        assert_leftovers_under_threshold(self._runs_root)
        await self._preflight(question_ids=question_ids)
        write_run_manifest(
            self._manifest, stratification=stratification, out_dir=self._results_dir
        )

        done = self._ledger.done_set()
        tally = {"complete": 0, "void": 0, "error": 0}
        skipped = 0
        for question_id in question_ids:
            if question_id in done:
                skipped += 1
                continue
            row, cost = await self._run_question(question_id)
            self._ledger.append(row)
            tally[row.status] += 1
            # After the row lands: a ceiling breach aborts the phase, and the
            # question that broke it is already recorded when it does.
            self._budget.record(cost)

        return PhaseReport(
            phase=phase,
            requested=len(question_ids),
            skipped=skipped,
            complete=tally["complete"],
            void=tally["void"],
            error=tally["error"],
            stratification=stratification,
            wall_seconds=time.monotonic() - started,
        )

    def _select(
        self, phase: Phase, strategy: StratificationStrategy | None
    ) -> tuple[list[str], SampleStratification | None]:
        """The phase's question ids, plus the stratification when it drew them."""
        if phase != "sample-s":
            if strategy is not None:
                raise StratificationError(
                    f"phase {phase!r} runs every question in the dataset; a stratification "
                    "strategy would not be applied and must not be passed"
                )
            return [str(raw["question_id"]) for raw in self._dataset.questions], None

        if strategy is None:
            raise StratificationError(
                "sample-s needs an explicit stratification strategy ('even' or "
                "'proportional'). Which one is right is an open question deferred until "
                "Oracle results show which types are weakest, so there is no default and "
                "the choice made is recorded in the run manifest"
            )
        sample = stratify_sample(self._dataset, strategy=strategy, size=self._sample_size)
        return list(sample.question_ids), sample

    async def _run_question(self, question_id: str) -> tuple[ResultRow, QuestionCost]:
        """Measure one question end to end and return its terminal row.

        The adapter is built outside the guarded section on purpose: the
        preflight already proved every question in the phase resolves its
        metadata and its date, so a failure here is a phase-level defect rather
        than one question's bad luck, and it aborts.
        """
        adapter = LongMemEvalAdapter(dataset=self._dataset, question_id=question_id)
        meta = adapter.question_meta()

        started = time.monotonic()
        meter = _Meter()
        status: Literal["complete", "void", "error"] = "complete"
        void_reason: VoidReason | None = None
        answer: Answer | None = None
        verdict: Verdict | None = None
        retrieval: dict[str, Any] | None = None

        run_dir = (self._runs_root / question_id).resolve()
        with WorkspaceLifecycle(run_dir, keep_on_failure=self._keep_on_failure) as workspace:
            try:
                answer, verdict, retrieval = await self._measure(
                    adapter, meta, question_id, run_dir, workspace, meter
                )
            except (GoldEvidenceFilteredError, TurnExceedsCapError) as exc:
                status, void_reason = "void", _VOID_REASONS[type(exc)]
                workspace.mark_failed("void")
                _LOG.warning("question %s voided (%s): %s", question_id, void_reason, exc)
            except Exception as exc:  # reason: one bad question errors its row, not the phase
                status = "error"
                workspace.mark_failed("error")
                # The row schema carries no error field, so the scrubbed text is
                # logged rather than written; an errored row is retried on resume.
                _LOG.error(
                    "question %s errored: %s", question_id, self._scrubber.scrub_exception(exc)
                )

        cost = meter.cost(question_id, wall_seconds=time.monotonic() - started)
        row = self._build_row(
            adapter=adapter,
            meta=meta,
            question_id=question_id,
            status=status,
            void_reason=void_reason,
            answer=answer,
            verdict=verdict,
            retrieval=retrieval,
            cost=cost,
        )
        return row, cost

    async def _measure(
        self,
        adapter: LongMemEvalAdapter,
        meta: QuestionMeta,
        question_id: str,
        run_dir: Path,
        workspace: WorkspaceLifecycle,
        meter: _Meter,
    ) -> tuple[Answer, Verdict, dict[str, Any] | None]:
        """Ingest, ask and grade one question, with only one agent ever live."""
        per_session = [
            self._chunker.split(session, session_idx=index)
            for index, session in enumerate(adapter.read())
        ]
        chunks = [chunk for session_chunks in per_session for chunk in session_chunks]

        agent = await self._agent_builder(question_id=question_id, run_dir=run_dir)
        metered = _MeteredAgent(agent, meter)
        try:
            if not workspace.resumed:
                await self._ingest(agent, metered, per_session, set(meta.has_answer_turns))
                workspace.mark_ingest_complete()
            answer = await self._asker.ask(metered, meta, chunks)
        finally:
            # Down before the judge is up: never two live agents (REQ-202).
            await agent.shutdown()

        verdict = await self._grade(run_dir, question_id, meta, answer, meter)
        return answer, verdict, _retrieval(question_id, meta, answer, chunks)

    async def _ingest(
        self,
        agent: HarnessAgent,
        metered: _MeteredAgent,
        per_session: Sequence[Sequence[Chunk]],
        gold_turn_ids: set[str],
    ) -> None:
        """Feed the haystack session by session, firing one pass at each boundary.

        The waiter is entered once for the whole question, because the
        ``arcmemory.consolidate`` logger it attaches to is the only surviving
        channel for the degrade warnings that turn de-duplication into a silent
        no-op — and those matter between passes as much as during one.
        """
        with self._waiter_builder(agent=agent, workspace=agent._workspace) as waiter:
            for session_chunks in per_session:
                report = await self._driver.ingest(
                    metered, session_chunks, gold_turn_ids=gold_turn_ids
                )
                for warning in report.warnings:
                    _LOG.warning("ingest: %s", warning)
                await waiter.wait()
            for warning in waiter.warnings:
                _LOG.warning("consolidation: %s", warning)

    async def _grade(
        self,
        run_dir: Path,
        question_id: str,
        meta: QuestionMeta,
        answer: Answer,
        meter: _Meter,
    ) -> Verdict:
        """Grade the answer on its own agent, built only once the eval agent is down."""
        judge_agent = await self._judge_builder(run_dir=run_dir)
        try:
            return await JudgeAgent(_MeteredAgent(judge_agent, meter)).judge(
                question_id=question_id,
                question_type=meta.question_type,
                question=meta.question,
                gold=meta.answer,
                answer=answer.text,
            )
        finally:
            await judge_agent.shutdown()

    def _build_row(
        self,
        *,
        adapter: LongMemEvalAdapter,
        meta: QuestionMeta,
        question_id: str,
        status: Literal["complete", "void", "error"],
        void_reason: VoidReason | None,
        answer: Answer | None,
        verdict: Verdict | None,
        retrieval: dict[str, Any] | None,
        cost: QuestionCost,
    ) -> ResultRow:
        """Assemble, scrub and validate the one row this question produces."""
        payload: dict[str, Any] = {
            "question_id": question_id,
            "status": status,
            "void_reason": void_reason,
            "question_type": meta.question_type,
            "is_abstention": is_abstention_question(question_id),
            "answer": "" if answer is None else answer.text,
            "verdict": None if verdict is None else verdict.model_dump(mode="json"),
            "retrieval": retrieval,
            "question_date": meta.question_date.isoformat(),
            "question_date_source": meta.question_date_source,
            "cost": cost.model_dump(mode="json"),
            "provenance": self._manifest.provenance.for_question(
                question_id,
                question_date_source=meta.question_date_source,
                session_ingest_order=adapter.session_ingest_order,
            ).model_dump(mode="json"),
        }
        return ResultRow.model_validate(self._scrubber.scrub_row(payload))


def _retrieval(
    question_id: str,
    meta: QuestionMeta,
    answer: Answer,
    chunks: Sequence[Chunk],
) -> dict[str, Any] | None:
    """The gold and retrieved evidence recall is scored from, or ``None`` for ``_abs``.

    The row carries the evidence rather than a per-question recall@k, because
    ``Answer.recalled_chunk_ids`` records *which* chunks the recall carried and
    not in what rank order — a recall@k computed from it would be an artifact of
    ingest order. COMP-012 owns that arithmetic and takes exactly these three
    lists. ``_abs`` questions name no answer session, so recall against them is
    either vacuously perfect or meaninglessly zero.
    """
    if is_abstention_question(question_id):
        return None
    by_coordinate = {f"{chunk.session_idx}:{chunk.chunk_idx}": chunk for chunk in chunks}
    return {
        "gold_turn_ids": list(meta.has_answer_turns),
        "gold_session_ids": list(meta.answer_session_ids),
        "retrieved_turn_ids": [
            turn_id
            for coordinate in answer.recalled_chunk_ids
            for turn_id in by_coordinate[coordinate].turn_ids
        ],
    }


__all__ = [
    "PHASES",
    "SAMPLE_S_SIZE",
    "AgentBuilder",
    "ConsolidationSession",
    "HarnessAgent",
    "JudgeBuilder",
    "Phase",
    "PhaseNotSelectedError",
    "PhaseReport",
    "PhaseRunner",
    "PreflightHook",
    "QuestionAsker",
    "SampleStratification",
    "StratificationError",
    "StratificationStrategy",
    "VoidReason",
    "WaiterBuilder",
    "resolve_phase",
    "stratify_sample",
    "write_run_manifest",
]
