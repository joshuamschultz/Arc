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
    EventExtraction,
    FactExtraction,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.mdfile import render_document
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, Procedure, Scope

_NOW = datetime(2026, 7, 7, 12, tzinfo=UTC)


class _NullDistiller:
    """A distiller that produces nothing — hygiene acts on existing files only."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return InsightMint()

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def extract_events(self, episodes: list[Event]) -> EventExtraction:
        return EventExtraction()

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
    store.write_fact(
        "joshua-schultz", "role", "founder", name="Joshua Schultz", entity_type="person"
    )
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


# -- (d) the one-type/orthogonal-tags migration runs itself each night ---------


class _ConfirmAll:
    async def confirm_entity_merges(self, groups: list) -> list[list[str]]:
        return [[ref.slug for ref in group] for group in groups]

    async def find_contradictions(self, group: list) -> list[str]:
        return []


class _Sink:
    def __init__(self) -> None:
        self.events: list = []

    def write(self, event: object) -> None:
        self.events.append(event)


async def test_the_nightly_pass_migrates_legacy_cards_without_an_operator_step(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    entities = workspace / "memory" / "entities"
    entities.mkdir(parents=True)
    (entities / "acme.md").write_text(
        render_document(
            {"name": "Acme", "entity_type": "organization", "tags": ["company", "doe"]},
            "# Acme\n\n## Facts\n- city: Austin .5 2026-07-01",
        ),
        encoding="utf-8",
    )
    store = _store(workspace, db, scope)
    store.write_fact("harness", "claim", "moat", name="Harness Advantage", entity_type="document")
    store.write_fact(
        "harness-thesis", "evidence", "3 wins", name="Harness Advantage", entity_type="thesis"
    )
    sink = _Sink()

    def nightly() -> Consolidator:
        return Consolidator(
            workspace=workspace,
            db=db,
            scope=scope,
            distiller=_NullDistiller(),
            config=MemoryConfig(),
            confirmer=_ConfirmAll(),
            audit_sink=sink,
        )

    await nightly().run_hygiene(now=_NOW)

    acme = store.read("acme")
    assert acme is not None and (acme.entity_type, acme.tags) == ("company", ["doe"])
    [harness] = [s for s in store.slugs() if s.startswith("harness")]
    card = store.read(harness)
    assert card is not None and card.entity_type == "thesis"
    assert {f.predicate for f in card.facts} == {"claim", "evidence"}
    actions = [e.action for e in sink.events]
    assert "memory.entity_kinds_normalized" in actions and "memory.entity_merged" in actions

    files = {p.name: p.read_text() for p in entities.glob("*.md")}
    await nightly().run_hygiene(now=_NOW + timedelta(days=1))
    assert {p.name: p.read_text() for p in entities.glob("*.md")} == files
