"""ArcMemoryBrain — the concrete Brain seam wraps capture/retrieve/consolidate."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.distill import (
    DaySummaryDraft,
    EventExtraction,
    FactCandidate,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.types import Event, Procedure

_DID = "did:arc:test-agent"


class _FakeDistiller:
    """Fixtured structured-completion seam (no network)."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction(
            facts=[FactCandidate(slug="ada", name="Ada", predicate="role", value="engineer")]
        )

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return InsightMint(
            insights=[
                InsightCandidate(
                    id="i1",
                    statement="retries beat crashes",
                    trigger="a transient failure recurs",
                    cues=["retry"],
                    instances=[e.event_id for e in events[:1]],
                )
            ]
        )

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def extract_events(self, episodes: list[Event]) -> EventExtraction:
        return EventExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft(timeline=["09:00 Ada worked on retries"], people=["Ada"])


async def test_capture_is_zero_llm_and_writes_only_the_raw_stream(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("Ada shipped the retry fix", kind="respond")
    # Capture writes the raw SQLite stream only; the curated daily-notes are a
    # consolidation output, so the fast path leaves no glass-box daily-log file.
    assert (workspace / "memory" / "index.db").exists()
    assert not (workspace / "memory" / "daily-log").exists()


async def test_retrieve_returns_boundary_marked_text(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("Ada owns the payments service", kind="respond")
    out = await brain.retrieve("who owns payments", top_k=5)
    assert isinstance(out, str)
    # Degraded (no embedder) still returns via BM25 + graph; boundary-marked when present.
    if out:
        assert "<memory-result" in out


async def test_retrieve_degrades_without_embedder_never_raises(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    # No capture, no embedder — must not raise, returns empty injectable text.
    assert await brain.retrieve("anything") == ""


async def test_retrieve_index_false_is_query_only_until_a_background_refresh(
    workspace: Path,
) -> None:
    """The turn's recall must never embed the corpus: index=False searches only.

    A captured card is not searchable until an index runs. With index=False the
    read path does no indexing, so the just-captured content stays invisible to
    recall until ``refresh_index`` (the background maintainer) indexes it. This is
    what keeps a whole-corpus embed off the turn/first-LLM-call path.
    """
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("Ada owns the payments service", kind="respond")

    # Query-only recall does not index, so the unindexed capture is not found.
    assert await brain.retrieve("who owns payments", index=False) == ""

    # The background refresh indexes the changed chunks; now query-only recall hits.
    await brain.refresh_index()
    assert "<memory-result" in await brain.retrieve("who owns payments", index=False)


async def test_retrieve_defaults_to_indexing_so_existing_callers_are_unchanged(
    workspace: Path,
) -> None:
    """index defaults True: a caller that does not opt out still auto-indexes."""
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("Ada owns the payments service", kind="respond")
    out = await brain.retrieve("who owns payments")  # no index kwarg → auto-index
    if out:
        assert "<memory-result" in out


async def test_consolidate_without_distiller_is_noop(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("something happened", kind="respond")
    result = await brain.consolidate()
    assert result["insights_minted"] == 0
    assert "episode_summary" in result


async def test_consolidate_with_distiller_mints_and_summarizes(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig(), distiller=_FakeDistiller())
    await brain.capture("Ada retried the transient failure and it worked", kind="respond")
    result = await brain.consolidate()
    assert result["insights_minted"] == 1
    assert result["facts_updated"] == 1
    assert result["days_summarized"] == 1
    assert "Consolidation:" in str(result["episode_summary"])
    # Consolidation — not capture — writes the curated daily-notes.
    daily = list((workspace / "memory" / "daily-log").glob("*.md"))
    assert daily, "consolidation must write a curated daily-notes file"
    assert "Ada worked on retries" in daily[0].read_text(encoding="utf-8")


class _CountingDistiller(_FakeDistiller):
    """A distiller that records how many times its fact extraction is invoked."""

    def __init__(self) -> None:
        self.fact_calls = 0

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        self.fact_calls += 1
        return await super().extract_facts(events)


async def test_consolidate_is_gated_by_interval(workspace: Path) -> None:
    """Consolidation runs on its cadence — a second call within the interval no-ops.

    Without the gate the slow LLM sleep-path re-ran every turn, hammering the
    distiller (and re-extracting the same entities). It must run once, then stay
    quiet until the configured interval elapses.
    """
    distiller = _CountingDistiller()
    brain = ArcMemoryBrain(
        workspace,
        _DID,
        config=MemoryConfig(consolidate_interval_minutes=60),
        distiller=distiller,
    )
    await brain.capture("Ada retried the transient failure and it worked", kind="respond")

    first = await brain.consolidate()
    assert first["facts_updated"] == 1
    assert distiller.fact_calls == 1

    # A second consolidate moments later is within the interval -> gated, no LLM.
    second = await brain.consolidate()
    assert distiller.fact_calls == 1  # distiller NOT called again
    assert second["facts_updated"] == 0


async def test_consolidate_escalates_to_nightly_hygiene_on_first_call(workspace: Path) -> None:
    """arcmemory owns cadence: the first consolidate of a local day runs hygiene.

    The heavier pass repairs bidirectional backlinks; observe the reciprocal link a
    plain light pass would not write. The hygiene stamp is persisted so arcagent stays
    ignorant of memory scheduling.
    """
    from arcmemory.index.graph import WeightedGraph
    from arcmemory.stores.semantic import SemanticStore

    brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig(), distiller=_FakeDistiller())
    store = SemanticStore(workspace, WeightedGraph(brain._db), scope=_DID)
    store.write_fact("alice", "role", "eng", name="Alice", entity_type="person")
    store.write_fact("acme", "kind", "co", name="Acme", entity_type="company")
    store.add_link("alice", "acme")  # one-directional

    await brain.consolidate()

    assert (workspace / "memory" / ".hygiene-last-run").exists()
    acme = store.read("acme")
    assert acme is not None and "[[alice]]" in acme.links_to  # reciprocal backlink repaired


async def test_holdings_publishes_unclassified_entity_pointers(workspace: Path) -> None:
    """holdings() surfaces what durable memory holds so a router can find this agent.

    Enumerates the semantic entity cards (each keyed by the proper noun that names
    it), skips classified cards, and returns a pointer line — never a full body.
    """
    from arcmemory.index.graph import WeightedGraph
    from arcmemory.stores.semantic import SemanticStore

    brain = ArcMemoryBrain(workspace, _DID)
    store = SemanticStore(workspace, WeightedGraph(brain._db), scope=_DID)
    store.write_fact("nnl", "requirements", "Rust toolchain and a signed SBOM", name="NNL")
    store.write_fact(
        "black-op", "detail", "need to know", name="Black Op", classification="secret"
    )

    holdings = await brain.holdings()

    joined = " || ".join(holdings)
    assert "NNL" in joined  # the proper noun that names the holding
    assert "Rust toolchain" in joined  # a fact snippet, enough for a lexical ranker
    assert "Black Op" not in joined  # a classified card never crosses into a digest


async def test_holdings_empty_without_memory(workspace: Path) -> None:
    """A fresh brain holds nothing to publish — no entities, no pointers."""
    brain = ArcMemoryBrain(workspace, _DID)
    assert await brain.holdings() == []


async def test_requires_identity() -> None:
    with pytest.raises(ValueError, match="agent_did"):
        ArcMemoryBrain(Path("."), "")


async def test_session_scope_isolates(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("session-a note", session_id="a")
    await brain.capture("session-b note", session_id="b")
    # Both scopes captured against one workspace DB without error (shared-nothing rows).
    assert (workspace / "memory" / "index.db").exists()
