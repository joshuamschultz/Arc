"""One type, orthogonal tags, real de-dup (operator items 6 and 7, 2026-10-04).

``entity_type`` is ONE kind from the closed taxonomy, the most specific that
applies. ``tags`` never repeat the type, its parents or any synonym of them. Two
cards for one real thing merge whatever their types: the survivor keeps the most
specific type, every fact, edge, alias and the folded card's history, and the
highest classification. "Not the same" is remembered. Re-runs change nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.distill import FactCandidate, FactExtraction, extract_facts
from arcmemory.entity_dedup import EntityDeduper, dedup_agent_memory
from arcmemory.entity_kind import kind_rank, more_specific_kind, normalize_card, parent_kinds
from arcmemory.hygiene import dedup_workspace
from arcmemory.index.graph import WeightedGraph
from arcmemory.mdfile import render_document
from arcmemory.operator import MemoryOperator, MutationStatus
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, Scope

_CONTEXT = Path(__file__).resolve().parents[2] / "src" / "arcmemory" / "context"


class ConfirmAll:
    """The LLM confirmer saying yes to every group it is asked about."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def confirm_entity_merges(self, groups: list[Any]) -> list[list[str]]:
        asked = [[ref.slug for ref in group] for group in groups]
        self.asked += asked
        return asked

    async def find_contradictions(self, group: list[Any]) -> list[str]:
        return []


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, action: str, target: str, extra: dict[str, Any]) -> None:
        self.events.append((action, target, extra))


def _store(workspace: Path, db: MemoryDB, scope: Scope) -> tuple[SemanticStore, WeightedGraph]:
    graph = WeightedGraph(db)
    return SemanticStore(workspace, graph, scope=scope.key), graph


def _deduper(
    store: SemanticStore,
    graph: WeightedGraph,
    scope: Scope,
    *,
    confirmer: Any = None,
    emit: Any = None,
) -> EntityDeduper:
    return EntityDeduper(
        store, graph, scope.key, config=MemoryConfig(), confirmer=confirmer, emit=emit
    )


def _files(workspace: Path) -> dict[str, str]:
    memory = workspace / "memory"
    if not memory.exists():
        return {}
    return {
        str(p.relative_to(memory)): p.read_text(encoding="utf-8")
        for p in sorted(memory.rglob("*"))
        if p.is_file() and p.suffix in {".md", ".json", ".jsonl"}
    }


def _two_harness_cards(store: SemanticStore) -> None:
    """The operator's case: one real thesis filed twice, once as a document."""
    store.write_fact(
        "harness-advantage",
        "claim",
        "the harness is the moat",
        name="Harness Advantage",
        entity_type="document",
        tags=["document", "arc", "roadmap"],
    )
    store.write_fact(
        "harness-advantage",
        "relates_to",
        "see [[arc-platform]]",
        name="Harness Advantage",
        entity_type="document",
    )
    store.write_fact(
        "harness-advantage-thesis",
        "evidence",
        "three customers chose the harness over the model",
        name="Harness Advantage",
        entity_type="thesis",
        tags=["thesis", "theses", "federal-sales"],
    )
    store.write_fact("arc-platform", "kind", "agent harness", name="Arc", entity_type="product")


# -- the taxonomy ----------------------------------------------------------------


def test_normalize_card_strips_the_type_its_parents_and_synonyms_from_tags() -> None:
    kind, tags = normalize_card(
        "thesis", ["thesis", "Theses", "document", "doc", "report", "arc", "doe", "arc"]
    )

    assert kind == "thesis"
    assert tags == ["arc", "doe"]


def test_normalize_card_upgrades_a_vague_type_from_a_category_tag() -> None:
    assert normalize_card("note", ["thesis", "arc"]) == ("thesis", ["arc"])
    assert normalize_card("thing", [], "Thesis 5: Tuning") == ("thesis", [])


def test_thesis_beats_document_beats_note() -> None:
    assert parent_kinds("thesis") == ("document",)
    assert kind_rank("thesis") > kind_rank("document") > kind_rank("note")
    assert more_specific_kind(["note", "document", "thesis"]) == "thesis"
    assert more_specific_kind(["thesis", "document"]) == "thesis"
    assert more_specific_kind(["document", "thesis"]) == "thesis"
    assert more_specific_kind(["note", "document"]) == "document"


# -- every write path normalizes -------------------------------------------------


def test_write_fact_never_stores_a_type_word_as_a_tag(workspace, db, scope) -> None:
    store, _ = _store(workspace, db, scope)

    store.write_fact("t", "p", "v", name="T", entity_type="thesis", tags=["document", "arc"])
    store.write_fact("t", "q", "w", entity_type="document", tags=["thesis", "doe"])

    card = store.read("t")
    assert card is not None
    assert card.entity_type == "thesis"  # a later, vaguer write never downgrades
    assert card.tags == ["arc", "doe"]


def test_set_identity_normalizes_type_and_tags(workspace, db, scope) -> None:
    store, _ = _store(workspace, db, scope)
    store.write_fact("t", "p", "v", name="T", entity_type="note")

    store.set_identity("t", entity_type="theses", tags=["Thesis", "document", "arc"])

    card = store.read("t")
    assert card is not None and (card.entity_type, card.tags) == ("thesis", ["arc"])


def test_workspace_slug_dedup_writes_a_normalized_card(workspace, db, scope) -> None:
    entities = workspace / "memory" / "entities"
    entities.mkdir(parents=True)
    for stem, kind, tags in (("Harness Plan", "thing", ["thesis"]), ("harness-plan", "doc", [])):
        (entities / f"{stem}.md").write_text(
            render_document(
                {"name": "Harness Plan", "entity_type": kind, "tags": [*tags, "arc"]},
                "# Harness Plan\n\n## Facts\n- p: v .5 2026-07-01",
            ),
            encoding="utf-8",
        )

    dedup_workspace(workspace, apply=True)

    store, _ = _store(workspace, db, scope)
    card = store.read("harness-plan")
    assert card is not None and (card.entity_type, card.tags) == ("thesis", ["arc"])


async def test_the_distiller_emits_topical_tags_that_land_normalized(workspace, db, scope) -> None:
    store, _ = _store(workspace, db, scope)

    class Distiller:
        async def extract_facts(self, events: list[Event]) -> FactExtraction:
            return FactExtraction(
                facts=[
                    FactCandidate(
                        slug="harness-advantage",
                        predicate="claim",
                        value="the harness is the moat",
                        name="Harness Advantage",
                        entity_type="thesis",
                        tags=["thesis", "document", "federal-sales"],
                    )
                ]
            )

    events = [Event(event_id="e1", scope=scope.key, kind="respond", text="harness talk")]
    await extract_facts(events, distiller=Distiller(), store=store, config=MemoryConfig())

    card = store.read("harness-advantage")
    assert card is not None and (card.entity_type, card.tags) == ("thesis", ["federal-sales"])


def test_the_stock_extraction_prompt_carries_the_type_and_tag_contract() -> None:
    prompt = (_CONTEXT / "distill_fact.md").read_text(encoding="utf-8")

    assert '"tags": [str]' in prompt
    assert "never repeat the entity_type" in prompt
    assert "most specific" in prompt


# -- de-dup across types -----------------------------------------------------------


async def test_a_cross_type_merge_keeps_facts_edges_and_provenance(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _two_harness_cards(store)

    result = await _deduper(store, graph, scope, confirmer=ConfirmAll()).run(apply=True)

    assert len(result.merged) == 1
    folded, survivor = result.merged[0]
    card = store.read(survivor)
    assert card is not None
    assert card.entity_type == "thesis"
    assert {f.predicate for f in card.facts} == {"claim", "relates_to", "evidence"}
    assert "[[arc-platform]]" in card.links_to
    assert "arc-platform" in {n for n, _ in graph.neighbors(scope.key, survivor)}
    assert folded in card.aliases
    assert sorted(card.tags) == ["arc", "federal-sales", "roadmap"]
    # Provenance: the folded card's own bytes survive in the merge record.
    [record] = [
        json.loads(line)
        for line in (workspace / "memory" / "merge-log.jsonl").read_text().splitlines()
    ]
    assert record["folded"] == folded and record["survivor"] == survivor
    assert "Harness Advantage" in record["folded_card"]
    assert record["folded_card"].count("- ") >= 1


async def test_merge_redirects_resolve_old_ids(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _two_harness_cards(store)
    [(folded, survivor)] = (
        await _deduper(store, graph, scope, confirmer=ConfirmAll()).run(apply=True)
    ).merged

    assert store.resolve(folded) == survivor
    operator = MemoryOperator(workspace, scope.agent_did)
    record = operator.get_entity(folded)
    assert record is not None and record.slug == survivor


async def test_a_merge_takes_the_highest_classification(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    store.write_fact(
        "nightfall", "site", "classified location", name="Nightfall Plan", entity_type="project"
    )
    store.write_fact("nightfall", "owner", "ops", name="Nightfall Plan", entity_type="project")
    store.write_fact(
        "nightfall-plan",
        "budget",
        "secret budget",
        name="Nightfall Plan",
        entity_type="document",
        classification="secret",
    )
    recorder = Recorder()

    result = await _deduper(store, graph, scope, confirmer=ConfirmAll(), emit=recorder).run(
        apply=True
    )

    [(_, survivor)] = result.merged
    card = store.read(survivor)
    assert card is not None and card.classification == "secret"
    assert {f.value for f in card.facts} == {"classified location", "ops", "secret budget"}
    merged = [extra for action, _, extra in recorder.events if action == "memory.entity_merged"]
    assert merged and merged[0]["classification"] == "secret"


async def test_a_cross_level_series_pair_is_never_folded_without_confirmation(
    workspace, db, scope
) -> None:
    store, graph = _store(workspace, db, scope)
    store.write_fact("thesis-7", "c", "a", name="Thesis 7", entity_type="thesis")
    store.write_fact(
        "thesis-7-byoa",
        "c",
        "b",
        name="Thesis 7: Bring Your Own Agent",
        entity_type="thesis",
        classification="cui",
    )

    plan = await _deduper(store, graph, scope).plan()

    assert plan.certain == []
    assert plan.ambiguous == [["thesis-7", "thesis-7-byoa"]]


async def test_not_the_same_is_remembered(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _two_harness_cards(store)
    deduper = _deduper(store, graph, scope, confirmer=ConfirmAll())
    [proposal] = await deduper.proposals()

    deduper.reject(list(proposal.slugs), actor_did="did:arc:operator")

    assert await deduper.proposals() == []
    assert (await deduper.run(apply=True)).merged == []
    # A fresh engine (tomorrow night's pass) reads the same remembered decision.
    again = _deduper(store, graph, scope, confirmer=ConfirmAll())
    assert await again.proposals() == []
    # ...and an alias of either side does not re-open the question.
    store.write_fact("harness-adv", "x", "y", name="Harness Adv", entity_type="thesis")
    store.merge_into("harness-advantage-thesis", "harness-adv", strict=False)
    assert all(
        set(p.slugs) != {"harness-advantage", "harness-advantage-thesis"}
        for p in await again.proposals()
    )


async def test_an_operator_merge_folds_a_proposal_without_an_llm(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _two_harness_cards(store)
    deduper = _deduper(store, graph, scope)
    [proposal] = await deduper.proposals()
    assert proposal.entity_type == "thesis"

    merged = deduper.merge(list(proposal.slugs), basis="operator")

    assert merged == [("harness-advantage-thesis", "harness-advantage")] or merged == [
        ("harness-advantage", "harness-advantage-thesis")
    ]
    assert len([s for s in store.slugs() if s.startswith("harness")]) == 1


async def test_a_rerun_is_idempotent(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _two_harness_cards(store)
    deduper = _deduper(store, graph, scope, confirmer=ConfirmAll())
    await deduper.run(apply=True)
    before = _files(workspace)

    second = await deduper.run(apply=True)

    assert second.merged == []
    assert _files(workspace) == before


async def test_the_migration_dry_run_changes_nothing(workspace, db, scope) -> None:
    store, _ = _store(workspace, db, scope)
    _two_harness_cards(store)
    legacy = workspace / "memory" / "entities" / "acme.md"
    legacy.write_text(
        render_document(
            {"name": "Acme", "entity_type": "organization", "tags": ["company", "doe"]},
            "# Acme\n\n## Facts\n- p: v .5 2026-07-01",
        ),
        encoding="utf-8",
    )
    before = _files(workspace)

    report = await dedup_agent_memory(workspace, scope.agent_did, apply=False)

    assert _files(workspace) == before
    assert [(c.slug, c.new_type, c.new_tags) for c in report.kinds.changes] == [
        ("acme", "company", ("doe",))
    ]
    assert [set(p.slugs) for p in report.proposals] == [
        {"harness-advantage", "harness-advantage-thesis"}
    ]


# -- the operator facade (what arcui calls) ----------------------------------------


async def test_operator_lists_merges_and_remembers_rejections(workspace, db, scope) -> None:
    store, _ = _store(workspace, db, scope)
    _two_harness_cards(store)
    store.write_fact("brad", "role", "cto", name="Brad Baker", entity_type="person")
    store.write_fact("brad-b", "city", "austin", name="Brad Baker", entity_type="person")
    operator = MemoryOperator(workspace, scope.agent_did)

    proposals = await operator.duplicate_proposals()
    assert {frozenset(p.slugs) for p in proposals} == {
        frozenset({"harness-advantage", "harness-advantage-thesis"}),
        frozenset({"brad", "brad-b"}),
    }

    rejected = operator.reject_duplicates(["brad", "brad-b"], actor_did="did:arc:operator")
    assert rejected.status is MutationStatus.APPLIED
    merged = operator.merge_duplicates(
        ["harness-advantage", "harness-advantage-thesis"], actor_did="did:arc:operator"
    )
    assert merged.status is MutationStatus.APPLIED

    assert await operator.duplicate_proposals() == []
    assert {"brad", "brad-b"} <= set(store.slugs())


async def test_operator_merge_refuses_cards_that_cannot_be_one_thing(workspace, db, scope) -> None:
    store, _ = _store(workspace, db, scope)
    store.write_fact("austin", "p", "v", name="Austin", entity_type="place")
    store.write_fact("austin-p", "p", "v", name="Austin", entity_type="person")
    operator = MemoryOperator(workspace, scope.agent_did)

    result = operator.merge_duplicates(["austin", "austin-p"], actor_did="did:arc:operator")

    assert result.status is MutationStatus.ERROR
    assert sorted(store.slugs()) == ["austin", "austin-p"]


@pytest.mark.parametrize("slugs", [[], ["only-one"], ["../etc", "x"]])
async def test_operator_merge_rejects_malformed_requests(workspace, scope, slugs) -> None:
    operator = MemoryOperator(workspace, scope.agent_did)

    result = operator.merge_duplicates(slugs, actor_did="did:arc:operator")

    assert result.status is MutationStatus.ERROR
