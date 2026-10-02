"""SPEC-083 — a publisher bound after construction reaches the promotion sweep.

The fleet attaches its shared-knowledge port to an agent AFTER the agent (and so
its brain) started. The brain therefore composes its sweep whenever promotion is
enabled and the tier allows it, and ``bind_promotion_publisher`` swaps the
publisher on the live sweep — the brain is never rebuilt.

Proves:

* no publisher yet -> ``publisher_unavailable`` with zero classifier calls and zero
  egress audit (the publisher gate precedes every classifier call);
* bind after construction -> the next consolidation classifies and publishes;
* bind ``None`` (detach) -> ``publisher_unavailable`` again, zero classifier calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcprompt import StockPromptSource
from arctrust.audit import AuditEvent
from arctrust.signer import InProcessSigner

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.consolidate import _HYGIENE_LAST_NAME
from arcmemory.distill import EventExtraction, FactExtraction, InsightMint, ProcedureExtraction
from arcmemory.promotion.classifier import (
    ClassifierInput,
    ClassifierVerdict,
    question_version,
)
from arcmemory.promotion.config import PromotionConfig
from arcmemory.promotion.question import load_promotion_question
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Event, Insight, Procedure

#: The packaged stock question (arcmemory/context/promotion_classify.md), parsed.
PROMOTION_QUESTION = load_promotion_question(StockPromptSource())

_DID = "did:arc:test-agent"


class _NullDistiller:
    """Hygiene (and so the sweep) only runs when a distiller is wired."""

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


def _brain(workspace: Path, classifier: _Classifier, sink: _DurableSink) -> ArcMemoryBrain:
    return ArcMemoryBrain(
        workspace,
        _DID,
        config=MemoryConfig.for_tier("personal"),
        distiller=_NullDistiller(),
        audit_sink=sink,
        promotion_config=PromotionConfig(enabled=True),
        promotion_classifier=classifier,
        promotion_publisher=None,
        promotion_signer=InProcessSigner(b"\x01" * 32),
    )


def _next_night(workspace: Path) -> None:
    """Clear the once-per-local-day hygiene stamp so the next consolidate sweeps."""
    (workspace / "memory" / _HYGIENE_LAST_NAME).unlink()


def _write_card(workspace: Path) -> None:
    InsightStore(workspace).write(
        Insight(id="acme-renewal", statement="Acme renewal closes at $42k/yr.", trigger="t")
    )


async def test_without_a_publisher_the_sweep_reports_unavailable_with_zero_egress(
    workspace: Path,
) -> None:
    _write_card(workspace)
    classifier, sink = _Classifier(), _DurableSink()

    summary = await _brain(workspace, classifier, sink).consolidate()

    assert summary["promotion_status"] == "publisher_unavailable"
    assert classifier.inputs == []
    assert "memory.promotion.egress" not in [e.action for e in sink.events]


async def test_publisher_bound_after_construction_publishes_on_next_consolidation(
    workspace: Path,
) -> None:
    _write_card(workspace)
    classifier, sink, publisher = _Classifier(), _DurableSink(), _Publisher()
    brain = _brain(workspace, classifier, sink)
    await brain.consolidate()

    brain.bind_promotion_publisher(publisher)
    _next_night(workspace)
    summary = await brain.consolidate()

    assert summary["promotion_status"] == "completed"
    assert publisher.references == ["insight:acme-renewal"]


async def test_unbinding_the_publisher_stops_egress_again(workspace: Path) -> None:
    _write_card(workspace)
    classifier, sink = _Classifier(), _DurableSink()
    brain = _brain(workspace, classifier, sink)
    brain.bind_promotion_publisher(_Publisher())

    brain.bind_promotion_publisher(None)
    summary = await brain.consolidate()

    assert summary["promotion_status"] == "publisher_unavailable"
    assert classifier.inputs == []


async def test_disabled_promotion_composes_no_sweep_and_binding_is_inert(
    workspace: Path,
) -> None:
    _write_card(workspace)
    classifier, publisher = _Classifier(), _Publisher()
    brain = ArcMemoryBrain(
        workspace,
        _DID,
        config=MemoryConfig.for_tier("personal"),
        distiller=_NullDistiller(),
        audit_sink=_DurableSink(),
    )

    brain.bind_promotion_publisher(publisher)
    summary = await brain.consolidate()

    assert summary["promotion_status"] is None
    assert classifier.inputs == [] and publisher.references == []
