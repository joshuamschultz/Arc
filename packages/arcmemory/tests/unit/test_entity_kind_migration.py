"""Kind/tag cleanup migration over legacy entity cards (arcmemory.hygiene)."""

from __future__ import annotations

from pathlib import Path

from arcmemory.db import MemoryDB
from arcmemory.hygiene import normalize_entity_kinds
from arcmemory.index.graph import WeightedGraph
from arcmemory.mdfile import render_document
from arcmemory.stores.semantic import SemanticStore


def _raw(workspace: Path, slug: str, name: str, entity_type: str, tags: list[str]) -> None:
    """Write a legacy card straight to disk, as the old importer did."""
    path = workspace / "memory" / "entities" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {"name": name, "entity_type": entity_type, "tags": tags, "entity_id": slug}
    path.write_text(
        render_document(frontmatter, f"# {name}\n\n## Facts\n- p: v .5 2026-07-01"),
        encoding="utf-8",
    )


def _legacy_store(workspace: Path, db: MemoryDB) -> SemanticStore:
    _raw(workspace, "thesis-5", "Thesis 5: Tuning", "thing", ["thesis"])
    _raw(workspace, "brad-baker", "Brad Baker", "person", ["people"])
    _raw(workspace, "acme", "Acme", "company", ["company", "doe-client"])
    _raw(workspace, "idea", "Vertical SaaS", "business-idea", [])
    _raw(workspace, "browserbase", "Browserbase", "thing", ["product"])
    _raw(workspace, "misc", "Misc Note", "unknown", [])
    _raw(workspace, "source-ab", "Source Ab", "source", [])
    return SemanticStore(workspace, WeightedGraph(db), scope="did:a")


def _kinds(store: SemanticStore) -> dict[str, tuple[str, list[str]]]:
    return {slug: (e.entity_type, e.tags) for slug in store.slugs() if (e := store.read(slug))}


def _files(workspace: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in (workspace / "memory" / "entities").glob("*.md")}


def test_migration_normalizes_types_and_drops_type_tags(workspace: Path, db: MemoryDB) -> None:
    store = _legacy_store(workspace, db)

    report = normalize_entity_kinds(store, apply=True)

    assert _kinds(store) == {
        "thesis-5": ("thesis", []),
        "brad-baker": ("person", []),
        "acme": ("company", ["doe-client"]),
        "idea": ("concept", []),
        "browserbase": ("product", []),
        "misc": ("other", []),
        "source-ab": ("source", []),
    }
    assert report.changed == 6


def test_migration_is_idempotent(workspace: Path, db: MemoryDB) -> None:
    store = _legacy_store(workspace, db)
    normalize_entity_kinds(store, apply=True)
    files = _files(workspace)

    second = normalize_entity_kinds(store, apply=True)

    assert second.changed == 0
    assert _files(workspace) == files


def test_dry_run_reports_without_writing(workspace: Path, db: MemoryDB) -> None:
    store = _legacy_store(workspace, db)
    before = _files(workspace)

    report = normalize_entity_kinds(store, apply=False)

    assert report.changed == 6
    changes = [(c.slug, c.old_type, c.new_type) for c in report.changes]
    assert ("thesis-5", "thing", "thesis") in changes
    assert _files(workspace) == before
