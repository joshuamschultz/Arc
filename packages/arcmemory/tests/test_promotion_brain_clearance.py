"""SPEC-083 T-1210 — the brain's sweep runs at the agent's real clearance.

The sweep only sends cards the shared side could accept at the writer's
clearance. That clearance must be the runtime identity's (the same label the
publisher's access carries), not a hardcoded ``unclassified``: a SECRET-cleared
agent's SECRET card is evaluated; an UNCLASSIFIED agent's SECRET card never is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcprompt import StockPromptSource
from arctrust import AgentIdentity
from arctrust.audit import AuditEvent
from arctrust.classification import Classification

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.distill import (
    EventExtraction,
    FactExtraction,
    InsightMint,
    ProcedureExtraction,
)
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
    host: str | None = None

    def __init__(self) -> None:
        self.inputs: list[ClassifierInput] = []

    async def ensure_available(self) -> None:
        return None

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        self.inputs.append(item)
        return ClassifierVerdict(
            label="agent_only",
            confidence=0.97,
            probabilities={"company": 0.01, "personal": 0.01, "agent_only": 0.97, "unclear": 0.01},
            personal_probability=0.01,
            classifier_id="jev",
            classifier_version="jev-1.13.0",
            request_id=None,
            input_tokens=None,
        )


class _Publisher:
    async def publish(self, reference: str, **_: Any) -> str:  # pragma: no cover
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


@pytest.mark.parametrize(
    ("clearance", "evaluated"),
    [(Classification.SECRET, ["acme-secret"]), (Classification.UNCLASSIFIED, [])],
)
async def test_sweep_uses_the_identity_clearance(
    workspace: Path, clearance: Classification, evaluated: list[str]
) -> None:
    identity = AgentIdentity.generate("test", "agent")
    identity.clearance = clearance
    InsightStore(workspace).write(
        Insight(
            id="acme-secret",
            statement="Acme renewal closes at $42k/yr.",
            trigger="t",
            classification="secret",
        )
    )
    classifier = _Classifier()
    brain = ArcMemoryBrain(
        workspace,
        identity.did,
        config=MemoryConfig.for_tier("personal"),
        distiller=_NullDistiller(),
        audit_sink=_DurableSink(),
        identity=identity,
        promotion_config=PromotionConfig(enabled=True),
        promotion_classifier=classifier,
        promotion_publisher=_Publisher(),
        promotion_signer=identity,
    )

    await brain.consolidate()

    assert [item.item_id for item in classifier.inputs] == evaluated
