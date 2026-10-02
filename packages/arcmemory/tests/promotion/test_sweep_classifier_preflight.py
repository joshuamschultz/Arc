"""SPEC-083 COMP-018 — classifier availability is settled BEFORE any egress record.

An unavailable classifier (drop-in removed, SDK or key missing) is a typed
``classifier_unavailable``, never ``classifier_error``:

- Preflight (``ensure_available``) runs before planning and before the durable
  ``memory.promotion.egress`` record, so an unavailable classifier leaves zero
  egress records and zero classify calls.
- If availability is lost between preflight and the call, the sweep reports
  ``classifier_unavailable``, publishes nothing, and durably records
  ``memory.promotion.egress_aborted`` so the egress "allow" is never the last word.

Real stores, real signed ledger; the classifier, publisher and sink are boundary fakes.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arcprompt import StockPromptSource
from arctrust.audit import AuditEvent
from arctrust.signer import InProcessSigner

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.promotion.classifier import (
    ClassifierInput,
    ClassifierUnavailableError,
    ClassifierVerdict,
    question_version,
)
from arcmemory.promotion.config import PromotionConfig
from arcmemory.promotion.ledger import PromotionLedger
from arcmemory.promotion.question import load_promotion_question
from arcmemory.promotion.sweep import PromotionSweep
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Insight, Scope

#: The packaged stock question (arcmemory/context/promotion_classify.md), parsed.
PROMOTION_QUESTION = load_promotion_question(StockPromptSource())

_NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
_EGRESS = "memory.promotion.egress"
_ABORTED = "memory.promotion.egress_aborted"


def _verdict() -> ClassifierVerdict:
    return ClassifierVerdict(
        label="company",
        confidence=0.97,
        probabilities={"company": 0.97, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01},
        personal_probability=0.02,
        classifier_id="jev",
        classifier_version="jev-1.13.0",
        request_id="req-1",
        input_tokens=10,
    )


class _Classifier:
    """Scripted boundary classifier. ``lose_on_call``: raise unavailable on that call."""

    classifier_id = "jev"
    question_version = question_version(PROMOTION_QUESTION)
    host: str | None = "api.typesafe.test"

    def __init__(
        self,
        *,
        preflight_unavailable: bool = False,
        preflight_hangs: bool = False,
        lose_on_call: int | None = None,
    ) -> None:
        self._preflight_unavailable = preflight_unavailable
        self._preflight_hangs = preflight_hangs
        self._lose_on_call = lose_on_call
        self.preflights = 0
        self.calls: list[str] = []

    async def ensure_available(self) -> None:
        self.preflights += 1
        if self._preflight_hangs:
            await asyncio.sleep(3600)
        if self._preflight_unavailable:
            raise ClassifierUnavailableError("typesafe-sdk is not installed")

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        self.calls.append(item.item_id)
        if self._lose_on_call is not None and len(self.calls) == self._lose_on_call:
            raise ClassifierUnavailableError("no API key in the vault")
        return _verdict()


class _Publisher:
    def __init__(self) -> None:
        self.references: list[str] = []

    async def publish(self, reference: str, **_: Any) -> str:
        self.references.append(reference)
        return f"shared:{reference}"

    async def demotions(self) -> dict[str, object]:
        return {}


class _DurableSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.durable: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)
        self.durable.append(event)

    def actions(self, action: str) -> list[AuditEvent]:
        return [event for event in self.events if event.action == action]


def _sweep(
    workspace: Path,
    db: MemoryDB,
    scope: Scope,
    classifier: _Classifier,
    publisher: _Publisher,
    sink: _DurableSink,
    *,
    timeout: float = 5.0,
) -> PromotionSweep:
    stores = SimpleNamespace(
        insights=InsightStore(workspace),
        procedures=ProceduralStore(workspace),
        entities=SemanticStore(workspace, WeightedGraph(db), scope=scope.key),
    )
    return PromotionSweep(
        cfg=PromotionConfig(enabled=True, request_timeout_seconds=timeout),
        tier="personal",
        stores=stores,
        ledger=PromotionLedger(workspace, InProcessSigner(b"\x02" * 32)),
        classifier=classifier,
        publisher=publisher,
        audit_sink=sink,
        clearance="unclassified",
    )


def _seed(workspace: Path, count: int = 3) -> list[str]:
    ids = [f"deal-{n}" for n in range(1, count + 1)]
    for n, item_id in enumerate(ids, start=1):
        InsightStore(workspace).write(
            Insight(id=item_id, statement=f"Deal {n} closes at ${n}0k/yr.", trigger="a renewal")
        )
    return ids


# -- preflight -----------------------------------------------------------------


async def test_unavailable_preflight_is_classifier_unavailable_with_zero_egress(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    _seed(workspace)
    classifier = _Classifier(preflight_unavailable=True)
    publisher, sink = _Publisher(), _DurableSink()

    result = await _sweep(workspace, db, scope, classifier, publisher, sink).run(_NOW)

    assert result.status == "classifier_unavailable"
    assert result.evaluated == 0
    assert classifier.preflights == 1
    assert classifier.calls == []
    assert sink.actions(_EGRESS) == []
    assert sink.durable == []
    assert publisher.references == []


async def test_hanging_preflight_is_bounded_and_classifier_unavailable(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    _seed(workspace)
    classifier = _Classifier(preflight_hangs=True)
    sink = _DurableSink()
    sweep = _sweep(workspace, db, scope, classifier, _Publisher(), sink, timeout=0.05)

    result = await asyncio.wait_for(sweep.run(_NOW), timeout=5.0)

    assert result.status == "classifier_unavailable"
    assert classifier.calls == []
    assert sink.actions(_EGRESS) == []


async def test_available_preflight_runs_once_before_the_egress_record(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    ids = _seed(workspace)
    classifier = _Classifier()
    publisher, sink = _Publisher(), _DurableSink()

    result = await _sweep(workspace, db, scope, classifier, publisher, sink).run(_NOW)

    assert result.status == "completed"
    assert classifier.preflights == 1
    assert sorted(classifier.calls) == ids
    assert [event.outcome for event in sink.actions(_EGRESS)] == ["allow"]
    assert sink.actions(_ABORTED) == []


# -- availability lost mid-classify ----------------------------------------------


async def test_unavailable_mid_classify_publishes_nothing_and_records_the_abort(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    _seed(workspace)
    classifier = _Classifier(lose_on_call=2)
    publisher, sink = _Publisher(), _DurableSink()

    result = await _sweep(workspace, db, scope, classifier, publisher, sink).run(_NOW)

    assert result.status == "classifier_unavailable"
    assert publisher.references == []
    # Only the first item actually reached the classifier.
    assert result.evaluated == 1
    egress_family = [e for e in sink.durable if e.action in (_EGRESS, _ABORTED)]
    assert [e.action for e in egress_family] == [_EGRESS, _ABORTED]
    aborted = egress_family[-1]
    sent, unsent = classifier.calls[:1], classifier.calls[1:]
    assert aborted.extra["sent_item_ids"] == sent
    assert aborted.extra["unsent_item_ids"] == [
        item_id for item_id in sink.actions(_EGRESS)[0].extra["item_ids"] if item_id not in sent
    ]
    assert unsent[0] in aborted.extra["unsent_item_ids"]


async def test_unavailable_on_first_call_records_that_nothing_was_sent(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    ids = _seed(workspace)
    classifier = _Classifier(lose_on_call=1)
    publisher, sink = _Publisher(), _DurableSink()

    result = await _sweep(workspace, db, scope, classifier, publisher, sink).run(_NOW)

    assert result.status == "classifier_unavailable"
    assert result.evaluated == 0
    (aborted,) = sink.actions(_ABORTED)
    assert aborted.extra["sent_item_ids"] == []
    assert sorted(aborted.extra["unsent_item_ids"]) == ids
    assert aborted in sink.durable


# -- constructor surface -----------------------------------------------------------


def test_sweep_takes_no_exporter() -> None:
    assert "exporter" not in inspect.signature(PromotionSweep).parameters
