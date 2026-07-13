"""WS3 — nightly hygiene consolidation (arcmemory owns the cadence, not arcagent).

``consolidate()`` escalates to a heavier hygiene pass on the first call after the
local date changes: bidirectional backlink repair + embedder-independent alias merge +
workspace dedup, all idempotent. The date decision lives in the ``Consolidator`` (a
``.hygiene-last-run`` stamp), so arcagent stays ignorant of memory scheduling.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from arcmemory.config import MemoryConfig
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import (
    DaySummaryDraft,
    FactExtraction,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, Scope

_NOW = datetime(2026, 7, 7, 12, tzinfo=UTC)


class _NullDistiller:
    """A distiller that produces nothing — hygiene acts on existing files only."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return InsightMint()

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None:
        return None


def _consolidator(workspace: Path, db: MemoryDB, scope: Scope) -> Consolidator:
    return Consolidator(
        workspace=workspace,
        db=db,
        scope=scope,
        distiller=_NullDistiller(),
        config=MemoryConfig(),
    )


def _store(workspace: Path, db: MemoryDB, scope: Scope) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db), scope=scope.key)


# -- (a) bidirectional backlink repair --------------------------------------


async def test_hygiene_writes_reciprocal_backlink_and_is_idempotent(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    store = _store(workspace, db, scope)
    store.write_fact("alice", "role", "eng", name="Alice", entity_type="person")
    store.write_fact("acme", "kind", "company", name="Acme", entity_type="company")
    store.add_link("alice", "acme")  # alice -> acme only

    assert "[[alice]]" not in (store.read("acme") or store.read("alice")).links_to

    await _consolidator(workspace, db, scope).run_hygiene(now=_NOW)

    acme = store.read("acme")
    assert acme is not None and "[[alice]]" in acme.links_to  # reciprocal backlink written

    # Idempotent: a second hygiene pass writes no further links.
    before = store.read("acme").links_to
    await _consolidator(workspace, db, scope).run_hygiene(now=_NOW + timedelta(days=1))
    assert store.read("acme").links_to == before


# -- (b) embedder-independent alias merge -----------------------------------


async def test_hygiene_merges_aliased_entities_without_embedder(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    store = _store(workspace, db, scope)
    # Survivor card that recorded "josh-schultz" as an alias of a prior fold.
    store.write_fact("joshua-schultz", "role", "founder", name="Joshua Schultz",
                     entity_type="person")
    survivor = store.read("joshua-schultz")
    assert survivor is not None
    survivor.aliases = ["josh-schultz"]
    store._persist(survivor)
    # A re-minted duplicate under the aliased slug.
    store.write_fact("josh-schultz", "city", "Austin", name="Josh Schultz", entity_type="person")

    merged = await _consolidator(workspace, db, scope).run_hygiene(now=_NOW)

    assert store.slugs() == ["joshua-schultz"]  # duplicate folded away, no embedder needed
    fused = store.read("joshua-schultz")
    assert fused is not None and {f.predicate for f in fused.facts} == {"role", "city"}
    assert merged is not None  # run_hygiene returns the light-pass result


# -- (c) once-per-local-day cadence -----------------------------------------


async def test_hygiene_due_only_after_local_date_changes(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    consolidator = _consolidator(workspace, db, scope)

    # Never run -> due.
    assert consolidator.hygiene_due(now=_NOW)
    await consolidator.run_hygiene(now=_NOW)

    # Same local day -> not due again.
    assert not consolidator.hygiene_due(now=_NOW + timedelta(hours=6))
    # Next local day -> due once more.
    assert consolidator.hygiene_due(now=_NOW + timedelta(days=1))


async def test_hygiene_stamp_survives_a_fresh_consolidator(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    await _consolidator(workspace, db, scope).run_hygiene(now=_NOW)
    fresh = _consolidator(workspace, db, scope)  # simulates an agent restart
    assert not fresh.hygiene_due(now=_NOW + timedelta(hours=1))
