"""T-1197 (SPEC-083 COMP-018 hook + T-1198 brain wiring) — the sweep inside nightly hygiene.

RED intent: ``arcmemory.promotion.sweep`` does not exist (``ModuleNotFoundError``),
and today's ``Consolidator`` / ``ConsolidationResult`` / ``ArcMemoryBrain`` have no
promotion seam at all.

Contract assumed (SDD COMP-018: "Hooked at the end of ``Consolidator.run_hygiene``,
inside ``try`` so a sweep failure never aborts hygiene or the hygiene stamp, and its
result is folded into the ``ConsolidationResult`` summary"). Names invented here
where the SDD names none:

- ``Consolidator(..., promotion_sweep=<obj with async run(now)>)`` — optional,
  default ``None`` (no sweep, no promotion fields set).
- ``ConsolidationResult`` gains ``promotion_status: str | None = None`` and the
  counts ``promotion_evaluated``, ``promotion_promoted``, ``promotion_kept_private``,
  ``promotion_blocked_secret``, ``promotion_too_large``, ``promotion_deferred``
  (ints, default 0). They surface verbatim in ``ArcMemoryBrain.consolidate()``'s
  summary dict (it is ``result.model_dump()``).
- A sweep that raises leaves ``promotion_status`` != ``"completed"`` (the SDD
  status set has no value for a crash; the builder may pick ``None`` or an error
  string) and emits nothing that claims success.
- ``ArcMemoryBrain(..., promotion_config=PromotionConfig, promotion_classifier=,
  promotion_publisher=, promotion_signer=)`` composes the sweep; the tier is the
  brain's own ``MemoryConfig.tier`` and the audit sink is the brain's sink.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arcprompt import StockPromptSource
from arctrust.audit import AuditEvent
from arctrust.signer import InProcessSigner

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import (
    DaySummaryDraft,
    EventExtraction,
    FactExtraction,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.promotion.classifier import (
    ClassifierInput,
    ClassifierVerdict,
    question_version,
)
from arcmemory.promotion.config import PromotionConfig
from arcmemory.promotion.question import load_promotion_question
from arcmemory.promotion.sweep import PromotionSweepResult
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, Insight, Procedure, Scope

#: The packaged stock question (arcmemory/context/promotion_classify.md), parsed.
PROMOTION_QUESTION = load_promotion_question(StockPromptSource())

_DID = "did:arc:test-agent"
_NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)


class _NullDistiller:
    """Produces nothing — hygiene acts on the files already on disk."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list[Any]) -> InsightMint:
        return InsightMint()

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def extract_events(self, episodes: list[Event]) -> EventExtraction:
        return EventExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None:
        return None


class _RecordingSweep:
    """Stands in for ``PromotionSweep``; snapshots hygiene state when it runs."""

    def __init__(
        self,
        *,
        result: PromotionSweepResult | None = None,
        exc: BaseException | None = None,
        probe: Any = None,
    ) -> None:
        self._result = result
        self._exc = exc
        self._probe = probe
        self.calls: list[datetime] = []
        self.snapshot: dict[str, Any] = {}

    async def run(self, now: datetime) -> PromotionSweepResult:
        self.calls.append(now)
        if self._probe is not None:
            self.snapshot = self._probe()
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _result(**overrides: Any) -> PromotionSweepResult:
    fields: dict[str, Any] = {
        "status": "completed",
        "evaluated": 5,
        "promoted": 2,
        "kept_private": 1,
        "blocked_secret": 1,
        "too_large": 1,
        "deferred": 3,
    }
    fields.update(overrides)
    return PromotionSweepResult(**fields)


def _consolidator(
    workspace: Path, db: MemoryDB, scope: Scope, sweep: object | None
) -> Consolidator:
    return Consolidator(
        workspace=workspace,
        db=db,
        scope=scope,
        distiller=_NullDistiller(),
        config=MemoryConfig(),
        promotion_sweep=sweep,
    )


# -- Consolidator hook ---------------------------------------------------------


async def test_run_hygiene_runs_sweep_last_after_merge_and_backlinks_before_stamp(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    store.write_fact("alice", "role", "eng", name="Alice", entity_type="person")
    store.write_fact("acme", "kind", "company", name="Acme", entity_type="company")
    store.add_link("alice", "acme")
    holder: dict[str, Consolidator] = {}

    def _probe() -> dict[str, Any]:
        acme = store.read("acme")
        return {
            "backlink_repaired": acme is not None and "[[alice]]" in acme.links_to,
            "hygiene_still_due": holder["c"].hygiene_due(now=_NOW),
        }

    sweep = _RecordingSweep(result=_result(), probe=_probe)
    consolidator = _consolidator(workspace, db, scope, sweep)
    holder["c"] = consolidator

    await consolidator.run_hygiene(now=_NOW)

    assert sweep.calls == [_NOW]
    assert sweep.snapshot == {"backlink_repaired": True, "hygiene_still_due": True}
    assert not consolidator.hygiene_due(now=_NOW)


async def test_run_hygiene_folds_sweep_result_into_consolidation_result(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    sweep = _RecordingSweep(result=_result())

    result = await _consolidator(workspace, db, scope, sweep).run_hygiene(now=_NOW)

    assert result.promotion_status == "completed"
    assert (
        result.promotion_evaluated,
        result.promotion_promoted,
        result.promotion_kept_private,
        result.promotion_blocked_secret,
        result.promotion_too_large,
        result.promotion_deferred,
    ) == (5, 2, 1, 1, 1, 3)


@pytest.mark.parametrize("status", ["disabled", "tier_forbidden", "classifier_error"])
async def test_run_hygiene_carries_non_completed_sweep_status(
    workspace: Path, db: MemoryDB, scope: Scope, status: str
) -> None:
    sweep = _RecordingSweep(result=_result(status=status, evaluated=0, promoted=0))

    result = await _consolidator(workspace, db, scope, sweep).run_hygiene(now=_NOW)

    assert result.promotion_status == status
    assert result.promotion_promoted == 0


async def test_run_hygiene_still_stamps_when_sweep_raises(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    sweep = _RecordingSweep(exc=RuntimeError("sweep exploded"))
    consolidator = _consolidator(workspace, db, scope, sweep)

    result = await consolidator.run_hygiene(now=_NOW)

    assert sweep.calls == [_NOW]
    assert not consolidator.hygiene_due(now=_NOW)
    assert result.promotion_status != "completed"
    assert result.promotion_promoted == 0


async def test_run_hygiene_without_sweep_leaves_promotion_fields_unset(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    consolidator = _consolidator(workspace, db, scope, None)

    result = await consolidator.run_hygiene(now=_NOW)

    assert result.promotion_status is None
    assert result.promotion_evaluated == 0
    assert not consolidator.hygiene_due(now=_NOW)


async def test_sweep_runs_once_per_local_day_with_hygiene(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    """The sweep rides hygiene's once-per-local-day gate (no second run the same day)."""
    # Local noon, so a two-hour gap never crosses the machine's local midnight.
    noon = datetime(2026, 9, 27, 12, 0).astimezone().astimezone(UTC)
    sweep = _RecordingSweep(result=_result())
    consolidator = _consolidator(workspace, db, scope, sweep)

    for later in (noon, noon + timedelta(hours=2)):
        if consolidator.hygiene_due(now=later):
            await consolidator.run_hygiene(now=later)

    assert sweep.calls == [noon]


# -- Brain wiring --------------------------------------------------------------


class _Classifier:
    classifier_id = "jev"
    question_version = question_version(PROMOTION_QUESTION)
    host: str | None = "api.typesafe.test"

    def __init__(self) -> None:
        self.inputs: list[ClassifierInput] = []

    async def ensure_available(self) -> None:
        return None

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        self.inputs.append(item)
        return ClassifierVerdict(
            label="company",
            confidence=0.97,
            probabilities={"company": 0.97, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01},
            personal_probability=0.02,
            classifier_id="jev",
            classifier_version="jev-1.13.0",
            request_id=None,
            input_tokens=None,
        )


class _Publisher:
    def __init__(self) -> None:
        self.references: list[str] = []

    async def publish(
        self, reference: str, *, content_sha256: str, confidence: float, classifier_version: str
    ) -> str:
        self.references.append(reference)
        return f"shared:{reference}"

    async def demotions(self) -> dict[str, object]:
        return {}


class _DurableSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)


def _brain(workspace: Path, tier: str, sink: _DurableSink, classifier: Any, publisher: Any):
    return ArcMemoryBrain(
        workspace,
        _DID,
        config=MemoryConfig.for_tier(tier),  # type: ignore[arg-type]  # reason: literal tier
        distiller=_NullDistiller(),
        audit_sink=sink,
        promotion_config=PromotionConfig(enabled=True),
        promotion_classifier=classifier,
        promotion_publisher=publisher,
        promotion_signer=InProcessSigner(b"\x01" * 32),
    )


async def test_brain_consolidate_runs_sweep_and_reports_it_in_summary(workspace: Path) -> None:
    InsightStore(workspace).write(
        Insight(id="acme-renewal", statement="Acme renewal closes at $42k/yr.", trigger="t")
    )
    classifier, publisher, sink = _Classifier(), _Publisher(), _DurableSink()

    summary = await _brain(workspace, "personal", sink, classifier, publisher).consolidate()

    assert summary["promotion_status"] == "completed"
    assert summary["promotion_promoted"] == 1
    assert publisher.references == ["insight:acme-renewal"]
    assert [e.action for e in sink.events].count("memory.promotion.egress") == 1


async def test_brain_at_federal_tier_reports_tier_forbidden_with_zero_calls(
    workspace: Path,
) -> None:
    InsightStore(workspace).write(
        Insight(id="acme-renewal", statement="Acme renewal closes at $42k/yr.", trigger="t")
    )
    classifier, publisher, sink = _Classifier(), _Publisher(), _DurableSink()

    summary = await _brain(workspace, "federal", sink, classifier, publisher).consolidate()

    assert summary["promotion_status"] == "tier_forbidden"
    assert classifier.inputs == []
    assert publisher.references == []
