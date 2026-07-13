"""WS2 — search-before-write identity resolution.

The distiller proposes free-text entity slugs, so "Joshua Schultz" and "Josh Schultz"
arrive as separate slugs. ``resolve_entity`` folds a candidate onto an existing card
IN ORDER: exact canonical file, recorded alias (closes the re-dup loop), embedding
fuzzy-match (same-type), LLM disambiguation — degrading to the raw canonical slug when
neither embedder nor distiller is wired, never raising.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.distill import extract_facts, resolve_entity
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, Scope


def _store(workspace: Path, db: MemoryDB, scope: Scope) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db), scope=scope.key)


async def test_resolve_exact_file_hit(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    store = _store(workspace, db, scope)
    store.write_fact("josh-schultz", "role", "founder", name="Josh Schultz", entity_type="person")
    resolved = await resolve_entity(
        store, slug="josh-schultz", name="Josh Schultz", entity_type="person"
    )
    assert resolved == "josh-schultz"


async def test_resolve_alias_hit_folds_variant(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    store = _store(workspace, db, scope)
    # Existing card whose survivor recorded "Joshua Schultz" as an alias of a prior fold.
    store.write_fact("josh-schultz", "role", "founder", name="Josh Schultz", entity_type="person")
    entity = store.read("josh-schultz")
    assert entity is not None
    entity.aliases = ["Joshua Schultz", "joshua-schultz"]
    store._persist(entity)

    resolved = await resolve_entity(
        store, slug="joshua-schultz", name="Joshua Schultz", entity_type="person"
    )
    assert resolved == "josh-schultz"  # aliased identity resolves onto the survivor


async def test_resolve_new_entity_without_embedder(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    store = _store(workspace, db, scope)
    # No embedder, no distiller, no prior card — must return the canonical slug, no raise.
    resolved = await resolve_entity(
        store, slug="Brand New Co", name="Brand New Co", entity_type="company"
    )
    assert resolved == "brand-new-co"


class _NameEmbedder:
    """Embeds entity NAMES so near-duplicate phrasings land on one vector."""

    _CLUSTERS: ClassVar[dict[str, int]] = {"acme": 0, "globex": 1}

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0, 0.0]
            for word, dim in self._CLUSTERS.items():
                if word in text.lower():
                    vec[dim] = 1.0
            out.append(vec)
        return out


async def test_resolve_fuzzy_same_type(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    store = _store(workspace, db, scope)
    store.write_fact("acme-corp", "hq", "Austin", name="Acme Corp", entity_type="company")
    resolved = await resolve_entity(
        store,
        slug="acme-incorporated",
        name="Acme Incorporated",
        entity_type="company",
        embedder=_NameEmbedder(),
    )
    assert resolved == "acme-corp"  # embedding fuzz folds the variant


async def test_resolve_fuzzy_never_crosses_type(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    store = _store(workspace, db, scope)
    store.write_fact("acme-corp", "hq", "Austin", name="Acme", entity_type="company")
    resolved = await resolve_entity(
        store,
        slug="acme-person",
        name="Acme",
        entity_type="person",  # different type — must NOT fold into the company
        embedder=_NameEmbedder(),
    )
    assert resolved == "acme-person"


class _BandEmbedder:
    """Puts the candidate in the ambiguous band (near, but below the merge threshold)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        # First text is the query; make each existing name ~0.7 cosine to it.
        return [[1.0, 0.0]] + [[0.7, 0.714] for _ in texts[1:]]


class _StubDisambiguator:
    """Distiller stub that only answers the disambiguation call (records it)."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.asked: list[tuple[str, list[str]]] = []

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None:
        self.asked.append((name, candidates))
        return self.answer


async def test_resolve_llm_disambiguation(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    store = _store(workspace, db, scope)
    store.write_fact("acme-corp", "hq", "Austin", name="Acme Corp", entity_type="company")
    disambiguator = _StubDisambiguator(answer="acme-corp")
    resolved = await resolve_entity(
        store,
        slug="a-c-m-e",
        name="ACME",
        entity_type="company",
        embedder=_BandEmbedder(),
        distiller=disambiguator,
    )
    assert resolved == "acme-corp"
    assert disambiguator.asked and disambiguator.asked[0][1] == ["acme-corp"]


# -- integration: the distiller write path resolves before writing ----------


class _FactOnlyDistiller:
    def __init__(self, facts: list) -> None:
        self._facts = facts

    async def extract_facts(self, events: list[Event]):  # type: ignore[no-untyped-def]
        from arcmemory.distill import FactExtraction

        return FactExtraction(facts=self._facts)


async def test_extract_facts_folds_aliased_slug_into_existing_card(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    from arcmemory.distill import FactCandidate

    store = _store(workspace, db, scope)
    store.write_fact("josh-schultz", "role", "founder", name="Josh Schultz", entity_type="person")
    entity = store.read("josh-schultz")
    assert entity is not None
    entity.aliases = ["joshua-schultz"]
    store._persist(entity)

    distiller = _FactOnlyDistiller(
        [FactCandidate(slug="joshua-schultz", predicate="city", value="Austin", name="Joshua Schultz",
                       entity_type="person")]
    )
    events = [Event(event_id="e0", scope=scope.key, kind="obs", text="joshua moved to austin")]
    applied = await extract_facts(
        events, distiller=distiller, store=store, config=MemoryConfig()
    )

    # The fact landed on the EXISTING card, and no duplicate was minted.
    assert store.slugs() == ["josh-schultz"]
    assert applied[0][0] == "josh-schultz"
    survivor = store.read("josh-schultz")
    assert survivor is not None
    assert {f.predicate for f in survivor.facts} == {"role", "city"}
