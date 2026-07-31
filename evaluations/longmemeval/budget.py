"""BudgetGovernor (COMP-021 / REQ-208, REQ-209, REQ-212).

Estimate before spending; abort when the estimate is exceeded.

*The estimate walks the real chunker* (REQ-208). ``n_calls`` is the number of
chunks the production ``TurnChunker`` actually produces from the production
``LongMemEvalAdapter``, not a guess from raw dataset size. That distinction is
the whole point: the chunker re-carries the session date on every chunk, packs
to a 1700 target rather than filling the 2000 cap, and voids a question whose
turn overflows the cap. A raw ``len(text) / 4`` sweep misses all three, and at
~40,000 calls a 20-30% error compounds into a meaningfully wrong ceiling.

*Characters still become tokens through a ratio, and that ratio is versioned.*
``evaluations/`` installs nothing and stays outside the uv workspace, so no
tokenizer is available to count exactly. The ratio therefore lives in the
pricing table beside the prices, is recorded with them, and matches the
framework's own accounting (``arcagent.core.session_internal.context``
estimates at 4 chars per token). Swapping in a real tokenizer later is a
pricing-table version bump, not a silent change to a past run's numbers.

*The pricing table lives here because the harness has nowhere else to put it.*
``evaluations/longmemeval/config/`` holds the rendered agent TOMLs, all of
which are gitignored so a run cannot leak credentials into the repo; a price
list must be committed and diffable, so it is committed as data in this module
and selected by version at the call site.

*The ceiling aborts, never warns* (REQ-209). ``record`` persists the row, logs
it, then raises ``SpendCeilingExceeded`` once the running total reaches 110% of
the estimate. Persisting first is deliberate: if the row that broke the ceiling
were lost with the process, a resume would start back under the ceiling and
spend it all over again.

*The five cost fields are logged per question* (REQ-212), to the ledger as well
as to the log, so a median-cost diff against the prior run catches an
ingest-cost regression before it burns the full-S budget.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict

from evaluations.ingest.chunker import TurnChunker, TurnExceedsCapError
from evaluations.longmemeval.adapter import LongMemEvalAdapter
from evaluations.longmemeval.dataset import Dataset, load_dataset

LOGGER_NAME: Final = "evaluations.budget"
_LOG = logging.getLogger(LOGGER_NAME)

MAX_EVENT_CHARS: Final = 2000
"""``arcmemory.security.sanitize`` truncates above this, so the chunker packs below it."""

CEILING_FRACTION: Final = 1.10
"""REQ-209: the run aborts once spend reaches 110% of the dry-run estimate."""


class UnknownPricingError(Exception):
    """A pricing table version or a model id is not in the shipped table.

    Raised rather than defaulted: an unpriced model estimates as free, which
    would leave the ceiling far above the real spend and silently useless.
    """


class DatasetUnavailableError(Exception):
    """The dataset file is not on disk.

    ``evaluations/data/`` is gitignored and the corpus is a manual download, so
    the common case is that it is simply absent. The dry run says so and stops;
    it never substitutes sample or synthetic questions, which would produce a
    confident estimate of a corpus nobody is going to run.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"dataset {path} is not present; download it into evaluations/data/ "
            "before estimating — the dry run will not invent one"
        )
        self.path = path


class SpendCeilingExceeded(Exception):  # noqa: N818  # reason: SDD COMP-021 names this symbol
    """Running spend reached the ceiling; the run aborts (REQ-209)."""

    def __init__(self, *, spent_usd: float, ceiling_usd: float, estimate_usd: float) -> None:
        super().__init__(
            f"spend ${spent_usd:.2f} reached the ceiling ${ceiling_usd:.2f} "
            f"({CEILING_FRACTION:.0%} of the ${estimate_usd:.2f} dry-run estimate); aborting"
        )
        self.spent_usd = spent_usd
        self.ceiling_usd = ceiling_usd
        self.estimate_usd = estimate_usd


# ---------------------------------------------------------------------------
# The versioned pricing table
# ---------------------------------------------------------------------------


class ModelRate(BaseModel):
    """USD per 1M tokens for one model id, as arcllm names it."""

    model_config = ConfigDict(frozen=True)

    input_per_1m: float
    output_per_1m: float


class PricingTable(BaseModel):
    """One dated snapshot of prices plus the tokenization ratio used with them.

    The ratio travels with the prices because both determine the ceiling, and a
    run is only comparable to another run that used the same pair.
    """

    model_config = ConfigDict(frozen=True)

    version: str
    chars_per_token: float
    rates: Mapping[str, ModelRate]

    def rate(self, model_id: str) -> ModelRate:
        """Return the rate for ``model_id`` or raise — never assume free."""
        try:
            return self.rates[model_id]
        except KeyError:
            raise UnknownPricingError(
                f"model {model_id!r} is not priced in table {self.version!r}; "
                f"known models: {sorted(self.rates)}"
            ) from None


CURRENT_PRICING_TABLE_VERSION: Final = "2026-07-30"

PRICING_TABLES: Final[Mapping[str, PricingTable]] = MappingProxyType(
    {
        CURRENT_PRICING_TABLE_VERSION: PricingTable(
            version=CURRENT_PRICING_TABLE_VERSION,
            chars_per_token=4.0,
            rates={
                "anthropic/claude-sonnet-4-5-20250929": ModelRate(
                    input_per_1m=3.00, output_per_1m=15.00
                ),
                "openai/gpt-4o-2024-08-06": ModelRate(input_per_1m=2.50, output_per_1m=10.00),
            },
        )
    }
)
"""Committed, diffable price snapshots keyed by version. Add a version, never edit one."""


def pricing_table(
    version: str, *, tables: Mapping[str, PricingTable] = PRICING_TABLES
) -> PricingTable:
    """Look up one pricing table version, raising if it was never shipped."""
    try:
        return tables[version]
    except KeyError:
        raise UnknownPricingError(
            f"no pricing table version {version!r}; known versions: {sorted(tables)}"
        ) from None


# ---------------------------------------------------------------------------
# The dry-run estimate
# ---------------------------------------------------------------------------


class CallProfile(BaseModel):
    """The per-call token allowances the real chunk count is multiplied by.

    Only ``n_calls`` and the ingest chunk text are measured; everything a chunk
    does *not* carry — the system prompt, the memory context injected on every
    turn, the consolidation window, the judge's prompt — is an allowance stated
    here so it is visible and adjustable rather than buried in arithmetic.

    Ingest dominates: at roughly 80 chunks per question it is ~99% of the calls,
    which is why the remaining allowances can stay this coarse.
    """

    model_config = ConfigDict(frozen=True)

    agent_model_id: str = "anthropic/claude-sonnet-4-5-20250929"
    judge_model_id: str = "openai/gpt-4o-2024-08-06"
    overhead_tokens_in: int = 1_200
    """System prompt plus injected memory context, re-sent on every agent turn."""
    agent_tokens_out: int = 200
    """One agent reply — an ingest acknowledgement, a distillation, or an answer."""
    consolidation_tokens_in: int = 6_000
    """One consolidation pass per session (D-499); its window is the cost driver."""
    query_tokens_in: int = 2_000
    """The question turn plus the recalled context it retrieves."""
    judge_tokens_in: int = 600
    """The vendored grading prompt with the question, gold answer and response."""
    judge_tokens_out: int = 10
    """The judge is pinned to ``max_tokens=10``."""


class Estimate(BaseModel):
    """What a dry run projects, with the pricing version that produced it."""

    model_config = ConfigDict(frozen=True)

    n_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    pricing_table_version: str
    n_questions: int
    n_sessions: int
    n_chunks: int
    n_voided_questions: int


def estimate_run(
    dataset: Dataset,
    *,
    profile: CallProfile,
    pricing_table_version: str = CURRENT_PRICING_TABLE_VERSION,
    tables: Mapping[str, PricingTable] = PRICING_TABLES,
    max_event_chars: int = MAX_EVENT_CHARS,
) -> Estimate:
    """Project the spend of running every question in ``dataset`` (REQ-208).

    Walks each question through the production adapter and the production
    ``TurnChunker``, so the projected call count is the call count the phase
    will make. A question the chunker refuses is counted as void and excluded
    from the projection: REQ-177 voids it rather than truncating its evidence,
    so it will not be run and must not inflate the ceiling.
    """
    pricing = pricing_table(pricing_table_version, tables=tables)
    agent_rate = pricing.rate(profile.agent_model_id)
    judge_rate = pricing.rate(profile.judge_model_id)
    chunker = TurnChunker(max_event_chars=max_event_chars)

    n_questions = 0
    n_voided = 0
    n_sessions = 0
    n_chunks = 0
    chunk_tokens = 0

    for raw in dataset.questions:
        adapter = LongMemEvalAdapter(dataset=dataset, question_id=raw["question_id"])
        try:
            chunks = [
                chunk
                for index, session in enumerate(adapter.read())
                for chunk in chunker.split(session, session_idx=index)
            ]
        except TurnExceedsCapError:
            n_voided += 1
            continue
        n_questions += 1
        n_sessions += len(adapter.session_ingest_order)
        n_chunks += len(chunks)
        chunk_tokens += sum(_tokens(chunk.text, pricing.chars_per_token) for chunk in chunks)

    agent_calls = n_chunks + n_sessions + n_questions
    agent_in = (
        chunk_tokens
        + (n_chunks + n_questions) * profile.overhead_tokens_in
        + n_sessions * profile.consolidation_tokens_in
        + n_questions * profile.query_tokens_in
    )
    agent_out = agent_calls * profile.agent_tokens_out
    judge_in = n_questions * profile.judge_tokens_in
    judge_out = n_questions * profile.judge_tokens_out

    cost = _cost(agent_in, agent_out, agent_rate) + _cost(judge_in, judge_out, judge_rate)

    return Estimate(
        n_calls=agent_calls + n_questions,
        tokens_in=agent_in + judge_in,
        tokens_out=agent_out + judge_out,
        cost_usd=cost,
        pricing_table_version=pricing.version,
        n_questions=n_questions,
        n_sessions=n_sessions,
        n_chunks=n_chunks,
        n_voided_questions=n_voided,
    )


def estimate_dataset_file(
    path: Path,
    *,
    revision: str,
    profile: CallProfile,
    expected_sha256: str | None = None,
    pricing_table_version: str = CURRENT_PRICING_TABLE_VERSION,
) -> Estimate:
    """Estimate straight from a dataset file, refusing when it is not there."""
    if not path.is_file():
        raise DatasetUnavailableError(path)
    dataset = load_dataset(path, expected_sha256=expected_sha256, revision=revision)
    return estimate_run(dataset, profile=profile, pricing_table_version=pricing_table_version)


def _tokens(text: str, chars_per_token: float) -> int:
    """Convert real chunk text to tokens at the pricing table's declared ratio."""
    return math.ceil(len(text) / chars_per_token)


def _cost(tokens_in: int, tokens_out: int, rate: ModelRate) -> float:
    return (tokens_in * rate.input_per_1m + tokens_out * rate.output_per_1m) / 1_000_000


# ---------------------------------------------------------------------------
# The governor
# ---------------------------------------------------------------------------


class QuestionCost(BaseModel):
    """One question's measured spend — the five fields REQ-212 requires."""

    model_config = ConfigDict(frozen=True)

    question_id: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    n_llm_calls: int
    wall_seconds: float


class BudgetGovernor:
    """Accumulate measured spend against a dry-run estimate and abort at 110%."""

    def __init__(self, *, estimate: Estimate, ledger_path: Path) -> None:
        """Replay any existing ledger, then refuse to start if it is already over.

        A resume that skipped the replay would hand the run a fresh budget every
        time it was killed — which is exactly the unbounded consumption the
        ceiling exists to stop (LLM10).
        """
        self._estimate = estimate
        self._ledger_path = ledger_path
        self._ceiling_usd = estimate.cost_usd * CEILING_FRACTION
        self._spent_usd = _replay(ledger_path)
        self._enforce()

    @property
    def spent_usd(self) -> float:
        """Total measured spend, including everything replayed from the ledger."""
        return self._spent_usd

    @property
    def ceiling_usd(self) -> float:
        """The abort threshold — 110% of the dry-run estimate."""
        return self._ceiling_usd

    def record(self, cost: QuestionCost) -> None:
        """Persist and log one question's cost, then abort if the ceiling is reached.

        The write happens before the check so the row that broke the ceiling is
        on disk when the process dies with the exception.
        """
        self._append(cost)
        self._spent_usd += cost.cost_usd
        _LOG.info(
            "question %s: tokens_in=%d tokens_out=%d cost_usd=%.6f n_llm_calls=%d "
            "wall_seconds=%.2f (spend $%.2f of ceiling $%.2f)",
            cost.question_id,
            cost.tokens_in,
            cost.tokens_out,
            cost.cost_usd,
            cost.n_llm_calls,
            cost.wall_seconds,
            self._spent_usd,
            self._ceiling_usd,
        )
        self._enforce()

    def _append(self, cost: QuestionCost) -> None:
        """Append one row, flushed and fsynced — a lost row understates the total."""
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(cost.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _enforce(self) -> None:
        if self._spent_usd >= self._ceiling_usd:
            raise SpendCeilingExceeded(
                spent_usd=self._spent_usd,
                ceiling_usd=self._ceiling_usd,
                estimate_usd=self._estimate.cost_usd,
            )


def _replay(ledger_path: Path) -> float:
    """Sum the ledger's recorded spend, tolerating a truncated final line.

    A half-written last line is the normal signature of a SIGKILL mid-append,
    not corruption; treating it as fatal would make the ceiling unresumable
    exactly when it matters.
    """
    if not ledger_path.exists():
        return 0.0
    total = 0.0
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            _LOG.warning("ignoring unparseable trailing line in %s", ledger_path)
            continue
        total += float(row["cost_usd"])
    return total
