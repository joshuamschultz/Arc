"""T-702 — arcmemory.operator facade: list/get/links/search/edit/metadata/delete.

The facade is the public read/mutation surface arcui consumes (COMP-001, REQ-084..
REQ-100). Every fixture DB here is built through arcmemory's OWN capture path
(``ArcMemoryBrain.capture`` for episodic entries, ``SemanticStore.write_fact`` for
entities + wiki-link edges) — never hand-inserted SQL — so these tests exercise the
same records production writes.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.brain import ArcMemoryBrain
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.operator import (
    EntityRecord,
    LinkRecord,
    MemoryOperator,
    MemoryPage,
    MemoryRecord,
    MutationResult,
    MutationStatus,
)
from arcmemory.stores.daily import DailyNotesStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import DaySummary, Insight, Procedure

_DID = "did:arc:op-agent"
_ACTOR = "did:arc:operator"
_VOCAB = ["alice", "bob", "carol"]


def _operator(workspace: Path) -> MemoryOperator:
    return MemoryOperator(workspace, _DID, seed_vocabulary=_VOCAB)


async def _seed_episodic(workspace: Path) -> ArcMemoryBrain:
    """Capture a handful of episodic memories through the real fast-capture path."""
    brain = ArcMemoryBrain(workspace, _DID, seed_vocabulary=_VOCAB)
    await brain.capture("alice met bob at the summit", kind="observation", salience=0.8)
    await brain.capture("carol reviewed the budget", kind="respond", salience=0.2)
    await brain.capture("the deployment finished cleanly", kind="tool")
    return brain


def _seed_entities(workspace: Path) -> None:
    """Write two entities + a wiki-link edge through the semantic store's own path."""
    store = SemanticStore(workspace, WeightedGraph(MemoryDB(workspace)), scope=_DID)
    store.write_fact("alice", "role", "lead engineer", confidence=0.9)
    store.write_fact("alice", "works-with", "[[bob]]", confidence=0.7)
    store.write_fact("bob", "role", "designer", confidence=0.6)


# -- REQ-084: list episodic memories, paged, with metadata --------------------


async def test_list_entries_returns_paged_records_with_metadata(workspace: Path) -> None:
    await _seed_episodic(workspace)
    page = _operator(workspace).list_entries(limit=2, offset=0)

    assert isinstance(page, MemoryPage)
    assert page.total == 3  # three captures
    assert page.limit == 2 and page.offset == 0
    assert len(page.items) == 2  # first page holds two

    record = page.items[0]
    assert isinstance(record, MemoryRecord)
    assert record.entry_id and record.text
    assert record.created  # ISO created timestamp (REQ-084)
    assert 1 <= record.importance <= 10  # bullet/importance score is 1..10 (REQ-084)
    assert 0.0 <= record.recency <= 1.0  # recency/decay indicator (REQ-084)
    assert record.source.endswith(".md")  # daily-log source reference (REQ-084)


async def test_list_entries_second_page_offsets(workspace: Path) -> None:
    await _seed_episodic(workspace)
    op = _operator(workspace)
    first = op.list_entries(limit=2, offset=0)
    second = op.list_entries(limit=2, offset=2)

    assert len(second.items) == 1  # 3 total, 2 already shown
    seen = {r.entry_id for r in first.items}
    assert second.items[0].entry_id not in seen  # no overlap across pages


async def test_capture_salience_drives_importance_score(workspace: Path) -> None:
    """A high-salience capture surfaces as a high 1..10 importance; a low one low."""
    await _seed_episodic(workspace)
    by_text = {r.text: r for r in _operator(workspace).list_entries(limit=50).items}

    high = by_text["alice met bob at the summit"]  # captured salience 0.8
    low = by_text["carol reviewed the budget"]  # captured salience 0.2
    assert high.importance > low.importance


# -- REQ-084: entities are listable with their own metadata -------------------


async def test_list_entities_returns_typed_records(workspace: Path) -> None:
    _seed_entities(workspace)
    entities = {e.slug: e for e in _operator(workspace).list_entities()}

    assert set(entities) == {"alice", "bob"}
    alice = entities["alice"]
    assert isinstance(alice, EntityRecord)
    # Entity-level confidence is the frontmatter default (write_fact sets the *fact*
    # confidence, not the entity's); the facade reports the real stored value.
    assert alice.confidence == 0.5
    assert alice.importance == 5  # 1..10 projection of confidence 0.5
    assert alice.source.endswith("alice.md")
    assert any("lead engineer .9" in fact for fact in alice.facts)


# -- REQ-084 / get single entry ----------------------------------------------


async def test_summary_counts_stream_entities_and_graph(workspace: Path) -> None:
    await _seed_episodic(workspace)
    _seed_entities(workspace)

    summary = _operator(workspace).summary()

    assert summary.episodic == 3  # three captured events
    assert summary.entities == 2  # alice.md, bob.md
    assert summary.graph_nodes >= 2  # alice, bob
    assert summary.graph_edges >= 1  # alice —[works-with]→ bob


async def test_summary_on_empty_workspace_is_all_zero(workspace: Path) -> None:
    summary = _operator(workspace).summary()
    assert summary.episodic == 0 and summary.entities == 0 and summary.graph_edges == 0


async def test_get_entry_roundtrips_a_single_memory(workspace: Path) -> None:
    await _seed_episodic(workspace)
    op = _operator(workspace)
    first = op.list_entries(limit=1).items[0]

    fetched = op.get_entry(first.entry_id)
    assert fetched is not None
    assert fetched.entry_id == first.entry_id
    assert fetched.text == first.text


async def test_get_entry_missing_returns_none(workspace: Path) -> None:
    await _seed_episodic(workspace)
    assert _operator(workspace).get_entry("does-not-exist") is None


# -- REQ-085: links are navigable --------------------------------------------


async def test_entity_links_expose_wiki_edges(workspace: Path) -> None:
    _seed_entities(workspace)
    links = _operator(workspace).links("alice")

    assert links, "alice links to bob via the wiki edge"
    assert all(isinstance(link, LinkRecord) for link in links)
    targets = {link.target_id for link in links}
    assert "bob" in targets
    bob_link = next(link for link in links if link.target_id == "bob")
    assert bob_link.target_type == "entity"  # navigable to an entity record


async def test_memory_links_point_to_tagged_entities(workspace: Path) -> None:
    """Selecting a memory shows its linked entities (REQ-085)."""
    await _seed_episodic(workspace)
    _seed_entities(workspace)
    op = _operator(workspace)
    entry = next(
        r for r in op.list_entries(limit=50).items if r.text == "alice met bob at the summit"
    )

    links = op.links(entry.entry_id)
    targets = {link.target_id for link in links}
    assert {"alice", "bob"} <= targets  # both tagged entities are navigable links


# -- REQ-086: search delegates to arcmemory's own retrieval ranking ----------


async def test_search_returns_ranked_hits(workspace: Path) -> None:
    await _seed_episodic(workspace)
    hits = await _operator(workspace).search("deployment", top_k=5)

    assert hits, "the deployment memory should surface"
    assert any("deployment" in hit.content for hit in hits)
    # Ranking passthrough: scores are monotonically non-increasing.
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


# -- REQ-088 / REQ-100: mutations are operator-gated and honest --------------


async def test_edit_entry_updates_text(workspace: Path) -> None:
    await _seed_episodic(workspace)
    op = _operator(workspace)
    entry = op.list_entries(limit=1).items[0]

    result = op.edit_entry(entry.entry_id, "corrected text", actor_did=_ACTOR)
    assert isinstance(result, MutationResult)
    assert result.status is MutationStatus.APPLIED
    assert result.actor_did == _ACTOR
    assert op.get_entry(entry.entry_id).text == "corrected text"


async def test_set_metadata_adjusts_importance(workspace: Path) -> None:
    await _seed_episodic(workspace)
    op = _operator(workspace)
    entry = next(
        r for r in op.list_entries(limit=50).items if r.text == "carol reviewed the budget"
    )
    assert entry.importance < 9  # started low (salience 0.2)

    result = op.set_metadata(entry.entry_id, actor_did=_ACTOR, importance=9)
    assert result.status is MutationStatus.APPLIED
    assert op.get_entry(entry.entry_id).importance == 9


async def test_delete_entry_removes_it(workspace: Path) -> None:
    await _seed_episodic(workspace)
    op = _operator(workspace)
    entry = op.list_entries(limit=1).items[0]

    result = op.delete_entry(entry.entry_id, actor_did=_ACTOR)
    assert result.status is MutationStatus.APPLIED
    assert op.get_entry(entry.entry_id) is None
    assert op.list_entries(limit=50).total == 2  # one fewer


# -- REQ-089: MutationResult is honest — applied | error, never partial -------


async def test_mutation_on_missing_entry_is_error_not_partial(workspace: Path) -> None:
    await _seed_episodic(workspace)
    op = _operator(workspace)

    result = op.edit_entry("ghost-id", "x", actor_did=_ACTOR)
    assert result.status is MutationStatus.ERROR
    assert result.error is not None and "ghost-id" in result.error
    # The store is untouched — no partial application.
    assert op.list_entries(limit=50).total == 3


async def test_delete_on_missing_entry_is_error(workspace: Path) -> None:
    await _seed_episodic(workspace)
    op = _operator(workspace)

    result = op.delete_entry("ghost-id", actor_did=_ACTOR)
    assert result.status is MutationStatus.ERROR
    assert op.list_entries(limit=50).total == 3


# -- U3/U4: curated glass-box reads — insights, procedures, daily notes -------


async def test_list_insights_returns_cards_sorted_by_id(workspace: Path) -> None:
    store = InsightStore(workspace)
    store.write(Insight(id="zulu-pattern", statement="z", trigger="t", confidence=0.6))
    store.write(Insight(id="alpha-pattern", statement="a", trigger="t", confidence=0.9))

    insights = _operator(workspace).list_insights()
    assert [i.id for i in insights] == ["alpha-pattern", "zulu-pattern"]
    assert isinstance(insights[0], Insight)


async def test_list_insights_empty_when_none_minted(workspace: Path) -> None:
    assert _operator(workspace).list_insights() == []


async def test_list_procedures_returns_cards_sorted_by_slug(workspace: Path) -> None:
    store = ProceduralStore(workspace)
    store.write(Procedure(slug="zulu", title="Zulu", steps=["a"], use_count=3))
    store.write(Procedure(slug="alpha", title="Alpha", steps=["b"], use_count=1))

    procedures = _operator(workspace).list_procedures()
    assert [p.slug for p in procedures] == ["alpha", "zulu"]
    assert isinstance(procedures[0], Procedure)


async def test_list_procedures_empty_when_none_promoted(workspace: Path) -> None:
    assert _operator(workspace).list_procedures() == []


async def test_list_daily_notes_newest_day_first(workspace: Path) -> None:
    store = DailyNotesStore(workspace)
    store.write(DaySummary(day="2026-07-05", timeline=["a"]))
    store.write(DaySummary(day="2026-07-07", timeline=["b"]))
    store.write(DaySummary(day="2026-07-06", timeline=["c"]))

    notes = _operator(workspace).list_daily_notes()
    assert [d.day for d in notes] == ["2026-07-07", "2026-07-06", "2026-07-05"]
    assert isinstance(notes[0], DaySummary)


async def test_list_daily_notes_empty_when_none_written(workspace: Path) -> None:
    assert _operator(workspace).list_daily_notes() == []


async def test_read_daily_note_roundtrips(workspace: Path) -> None:
    store = DailyNotesStore(workspace)
    store.write(DaySummary(day="2026-07-07", timeline=["shipped the release"]))

    note = _operator(workspace).read_daily_note("2026-07-07")
    assert note is not None
    assert note.timeline == ["shipped the release"]


async def test_read_daily_note_missing_returns_none(workspace: Path) -> None:
    assert _operator(workspace).read_daily_note("2026-01-01") is None
