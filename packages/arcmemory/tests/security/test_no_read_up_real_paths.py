"""F1 — no-read-up on the REAL capture→retrieve paths (not just the gate unit).

The unit tests in ``test_security.py`` feed hand-built ``Recall`` objects (with a
classification already set) straight into ``gate_no_read_up``. That never exercises
the paths where the label is *threaded*: an episodic event, a curated daily-notes
file, and an insight minted from them. This module drives the production
``ArcMemoryBrain`` end to end — ``capture(text, classification="SECRET")`` then
``consolidate()`` then ``retrieve(clearance=…)`` — and proves a SECRET memory is
dropped for an UNCLASSIFIED caller on **every** channel (episodic, daily-notes file,
minted insight), that a missing label **fails closed** at federal, and that a dropped
item leaks nothing into the returned bundle.
"""

from __future__ import annotations

from pathlib import Path

from arctrust.audit import AuditEvent
from arctrust.classification import Classification

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.db import DEFAULT_DIMS
from arcmemory.distill import (
    DaySummaryDraft,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.types import Event

_DID = "did:arc:secret-agent"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class ConceptEmbedder:
    """Deterministic embedder: any text mentioning 'override' hits one dimension."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * DEFAULT_DIMS
            if "override" in text.lower() or "asserted" in text.lower():
                vec[0] = 1.0
            out.append(vec)
        return out


class SecretInsightDistiller:
    """Mints one insight whose instances are exactly the (secret) captured events."""

    def __init__(self, statement: str, trigger: str) -> None:
        self._statement = statement
        self._trigger = trigger

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return InsightMint(
            insights=[
                InsightCandidate(
                    id="secret-pattern",
                    statement=self._statement,
                    trigger=self._trigger,
                    cues=["guarded-procedure"],
                    instances=[e.event_id for e in events],
                )
            ]
        )

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        # The day's curated notes echo the sensitive term; the day is SECRET-labeled
        # (dominating label of its events), so this file channel must be gated too.
        return DaySummaryDraft(timeline=["the launch override code was discussed"])


def _dropped_sources(sink: RecordingSink) -> list[str]:
    return [str(e.extra.get("source")) for e in sink.events if e.action == "recall.dropped"]


async def test_secret_episodic_and_daily_log_dropped_for_unclassified(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(
        workspace,
        _DID,
        distiller=SecretInsightDistiller(
            statement="A guarded override procedure recurs across shifts.",
            trigger="a protected override sequence is asserted",
        ),
        audit_sink=sink,
    )  # BM25 + graph (no embedder)

    await brain.capture(
        "the launch override code is alpha-seven", kind="obs", classification="SECRET"
    )
    await brain.consolidate()  # writes a SECRET-labeled curated daily-notes file
    text = await brain.retrieve(
        "launch override code", clearance=Classification.UNCLASSIFIED.name, top_k=5, budget=10_000
    )

    # The SECRET text never reaches an UNCLASSIFIED caller — via EITHER the raw
    # episodic chunk OR the curated daily-notes file chunk (both carried the real label).
    assert "alpha-seven" not in text
    assert "override" not in text
    dropped = _dropped_sources(sink)
    assert any(s.startswith("event:") for s in dropped), "episodic stream leak not gated"
    assert any("daily-log" in s for s in dropped), "daily-notes file channel leak not gated"


async def test_secret_insight_dropped_for_unclassified(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(
        workspace,
        _DID,
        embedder=ConceptEmbedder(),
        distiller=SecretInsightDistiller(
            statement="A guarded override procedure recurs across shifts.",
            trigger="a protected override sequence is asserted",
        ),
        audit_sink=sink,
    )

    await brain.capture(
        "the reactor override sequence is asserted nightly", kind="obs", classification="SECRET"
    )
    await brain.consolidate()  # mints an insight whose instances are the SECRET event
    text = await brain.retrieve(
        "override procedure", clearance=Classification.UNCLASSIFIED.name, top_k=5, budget=10_000
    )

    # The insight inherited the MAX classification of its instances (SECRET) and is
    # dropped — the abstraction cannot launder a SECRET episode to an UNCLASSIFIED caller.
    assert "guarded override procedure" not in text
    assert "secret-pattern" not in text
    assert any(e.action == "recall.dropped" for e in sink.events)


async def test_unlabeled_episodic_fails_closed_at_federal(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(
        workspace, _DID, config=MemoryConfig.for_tier("federal"), audit_sink=sink
    )

    # Empty label == "unknown provenance": at federal it must fail closed on the
    # raw episodic stream, even for a TOP_SECRET caller.
    await brain.capture("the widget of unknown provenance", kind="obs", classification="")
    text = await brain.retrieve(
        "widget provenance", clearance=Classification.TOP_SECRET.name, top_k=5, budget=10_000
    )

    assert "provenance" not in text
    assert any(e.extra.get("reason") == "unlabeled_fail_closed" for e in sink.events)


async def test_unlabeled_insight_fails_closed_at_federal(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(
        workspace,
        _DID,
        config=MemoryConfig.for_tier("federal"),
        embedder=ConceptEmbedder(),
        distiller=SecretInsightDistiller(
            statement="An asserted override with unknown provenance recurs.",
            trigger="a protected override sequence is asserted",
        ),
        audit_sink=sink,
    )

    await brain.capture("the override step is asserted", kind="obs", classification="")
    await brain.consolidate()  # insight inherits the unknown label of its sole instance
    text = await brain.retrieve(
        "override procedure", clearance=Classification.TOP_SECRET.name, top_k=5, budget=10_000
    )

    assert "unknown provenance" not in text
    assert any(e.extra.get("reason") == "unlabeled_fail_closed" for e in sink.events)
