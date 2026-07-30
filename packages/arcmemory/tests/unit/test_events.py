"""Events — the card for a thing that happened in the USER's life.

Four-way split this store completes: procedures are how *we* do things, events are
what happened to the *user*, daily notes are what the *agent* did, policy is how the
agent improves. Nothing before this recorded that a thing OCCURRED — when, who was in
it, and how it came out — so "what happened with this client this quarter" was
unanswerable.

Pins: markdown round-trip (the card IS the truth), minting from a conversation,
participants becoming shared-graph edges, those edges surviving an index rebuild, and
the agentic ``record_event`` tool (the DEFAULT consolidation engine's only write path).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from arcmemory.config import MemoryConfig
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import (
    DaySummaryDraft,
    EventCandidate,
    EventExtraction,
    FactExtraction,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import IndexRebuilder
from arcmemory.index.source import iter_source_chunks
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.events import EventStore
from arcmemory.tools import build_memory_tools
from arcmemory.types import Event, LifeEvent, Scope

_NOW = datetime(2026, 7, 7, tzinfo=UTC)
_DID = "did:arc:agent:tester"


class EventDistiller:
    """Distiller stub that proposes ONE life event and nothing else."""

    def __init__(self, extraction: EventExtraction | None = None) -> None:
        self._extraction = extraction or EventExtraction(
            events=[
                EventCandidate(
                    slug="q3-kickoff-with-acme",
                    title="Q3 kickoff with Acme",
                    date="2026-07-06",
                    event_type="meeting",
                    participants=["alice", "[[acme]]"],
                    summary="walked Acme through the Q3 plan",
                    outcome="Acme agreed to a paid pilot",
                )
            ]
        )

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return InsightMint()

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def extract_events(self, episodes: list[Event]) -> EventExtraction:
        return self._extraction

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None:
        return None

    async def confirm_entity_merges(self, groups: list) -> list[list[str]]:
        return []


def _seed_conversation(db: MemoryDB, workspace: Path, scope: Scope) -> None:
    """A short session conversation the event is learned from."""
    episodic = EpisodicStore(db, workspace)
    for i, text in enumerate(
        [
            "we ran the Q3 kickoff with Acme yesterday",
            "alice walked them through the plan and they agreed to a paid pilot",
        ]
    ):
        episodic.append(
            Event(
                event_id=f"e{i}",
                scope=scope.key,
                kind="user" if i == 0 else "respond",
                text=text,
                ts=f"2026-07-07T00:00:0{i}+00:00",
            )
        )


def _consolidator(workspace: Path, db: MemoryDB, scope: Scope) -> Consolidator:
    return Consolidator(
        db,
        workspace,
        scope,
        distiller=EventDistiller(),
        config=MemoryConfig(consolidate_engine="pipeline"),
    )


# ---------------------------------------------------------------------------
# The card + its markdown truth
# ---------------------------------------------------------------------------


class TestEventCard:
    def test_event_card_round_trips_through_markdown(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path)
        card = LifeEvent(
            slug="q3-kickoff-with-acme",
            title="Q3 kickoff with Acme",
            date="2026-07-06",
            recorded="2026-07-07",
            event_type="meeting",
            participants=["alice", "acme"],
            summary="walked Acme through the Q3 plan",
            outcome="Acme agreed to a paid pilot",
            classification="unclassified",
        )
        path = store.write(card)

        assert path == tmp_path / "memory" / "events" / "q3-kickoff-with-acme.md"
        assert store.read("q3-kickoff-with-acme") == card

    def test_participants_render_as_wiki_links(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path)
        store.write(LifeEvent(slug="deal-closed", title="Deal closed", participants=["alice"]))
        text = store.path_for("deal-closed").read_text(encoding="utf-8")
        assert "[[alice]]" in text

    def test_date_happened_is_distinct_from_recorded(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path)
        card = store.upsert("board-meeting", "Board meeting", date="2026-01-15")
        assert card.date == "2026-01-15"
        assert card.recorded == datetime.now(UTC).strftime("%Y-%m-%d")
        assert card.recorded != card.date

    def test_upsert_canonicalizes_slug_and_unions_participants(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path)
        store.upsert("Q3 Kickoff!", "Q3 kickoff", participants=["alice"])
        card = store.upsert("q3-kickoff", "Q3 kickoff", participants=["[[bob]]", "alice"])

        assert card.slug == "q3-kickoff"
        assert card.participants == ["alice", "bob"]
        assert store.slugs() == ["q3-kickoff"]

    def test_upsert_never_lowers_classification(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path)
        store.upsert("site-visit", "Site visit", classification="secret")
        card = store.upsert("site-visit", "Site visit", classification="unclassified")
        assert card.classification == "secret"

    def test_read_missing_card_is_none(self, tmp_path: Path) -> None:
        assert EventStore(tmp_path).read("nope") is None
        assert EventStore(tmp_path).slugs() == []


# ---------------------------------------------------------------------------
# Minting from a conversation (the distiller seam wired into consolidation)
# ---------------------------------------------------------------------------


class TestMintingFromConversation:
    @pytest.mark.asyncio
    async def test_consolidation_mints_event_card_from_conversation(self, tmp_path: Path) -> None:
        db = MemoryDB(tmp_path)
        scope = Scope(agent_did=_DID)
        _seed_conversation(db, tmp_path, scope)

        result = await _consolidator(tmp_path, db, scope).run(now=_NOW)

        assert result.events_recorded == 1
        card = EventStore(tmp_path).read("q3-kickoff-with-acme")
        assert card is not None
        assert card.title == "Q3 kickoff with Acme"
        assert card.date == "2026-07-06"
        assert card.event_type == "meeting"
        assert card.outcome == "Acme agreed to a paid pilot"

    @pytest.mark.asyncio
    async def test_event_participants_become_graph_edges(self, tmp_path: Path) -> None:
        db = MemoryDB(tmp_path)
        scope = Scope(agent_did=_DID)
        _seed_conversation(db, tmp_path, scope)

        await _consolidator(tmp_path, db, scope).run(now=_NOW)

        edges = WeightedGraph(db).neighbor_edges(scope.key, "q3-kickoff-with-acme")
        assert {target for target, kind, _w in edges if kind == "link"} == {"alice", "acme"}

    @pytest.mark.asyncio
    async def test_event_card_is_a_retrievable_source_chunk(self, tmp_path: Path) -> None:
        db = MemoryDB(tmp_path)
        scope = Scope(agent_did=_DID)
        _seed_conversation(db, tmp_path, scope)

        await _consolidator(tmp_path, db, scope).run(now=_NOW)

        chunks = list(iter_source_chunks(tmp_path / "memory", tmp_path, []))
        assert any(c.source_path == "memory/events/q3-kickoff-with-acme.md" for c in chunks)


# ---------------------------------------------------------------------------
# The index is disposable: rebuilding must not drop event edges
# ---------------------------------------------------------------------------


class TestRebuild:
    @pytest.mark.asyncio
    async def test_event_edges_survive_an_index_rebuild(self, tmp_path: Path) -> None:
        db = MemoryDB(tmp_path)
        scope = Scope(agent_did=_DID)
        EventStore(tmp_path).upsert(
            "q3-kickoff-with-acme",
            "Q3 kickoff with Acme",
            date="2026-07-06",
            participants=["alice", "acme"],
        )

        await IndexRebuilder(db, tmp_path, scope).rebuild()

        edges = WeightedGraph(db).neighbor_edges(scope.key, "q3-kickoff-with-acme")
        assert {target for target, kind, _w in edges if kind == "link"} == {"alice", "acme"}


# ---------------------------------------------------------------------------
# The agentic engine (the DEFAULT) writes only through tools
# ---------------------------------------------------------------------------


class TestRecordEventTool:
    @pytest.mark.asyncio
    async def test_record_event_tool_writes_card_and_links_participants(
        self, tmp_path: Path
    ) -> None:
        db = MemoryDB(tmp_path)
        scope = Scope(agent_did=_DID)
        tools = build_memory_tools(
            workspace=tmp_path, db=db, config=MemoryConfig(), caller_did=_DID
        )
        record = next(t for t in tools if t.name == "record_event")

        out = await record.execute(
            {
                "slug": "q3-kickoff-with-acme",
                "title": "Q3 kickoff with Acme",
                "date": "2026-07-06",
                "event_type": "meeting",
                "participants": ["alice"],
                "outcome": "paid pilot agreed",
            }
        )

        assert "q3-kickoff-with-acme" in out
        card = EventStore(tmp_path).read("q3-kickoff-with-acme")
        assert card is not None and card.outcome == "paid pilot agreed"
        edges = WeightedGraph(db).neighbor_edges(scope.key, "q3-kickoff-with-acme")
        assert ("alice", "link", 1.0) in edges

    @pytest.mark.asyncio
    async def test_record_event_tool_skips_a_titleless_event(self, tmp_path: Path) -> None:
        tools = build_memory_tools(
            workspace=tmp_path, db=MemoryDB(tmp_path), config=MemoryConfig(), caller_did=_DID
        )
        record = next(t for t in tools if t.name == "record_event")
        assert "skipped" in await record.execute({"slug": "", "title": ""})
