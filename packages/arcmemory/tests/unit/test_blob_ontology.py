"""SPEC-073 COMP-009 (T-1034/T-1035) — blob ontology discovery walker: a coarse
folder/type catalog written as Entity+Fact, O(folders) not O(objects).

RED: ``arcmemory.blob_ontology`` does not exist yet — every test fails on
import (ModuleNotFoundError), a feature-absent reason, not a typo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.blob_ontology import BlobObject, walk_blob_source
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore


def _store(workspace: Path, db: MemoryDB) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db), scope="did:a")


def _mixed_objects() -> list[BlobObject]:
    """~5 objects across 2 immediate folders, mixed mime types within a folder."""
    return [
        BlobObject(path="reports/2026/q1.pdf", mime="application/pdf", size=1000),
        BlobObject(path="reports/2026/q2.pdf", mime="application/pdf", size=1200),
        BlobObject(path="reports/2026/notes.txt", mime="text/plain", size=50),
        BlobObject(path="photos/vacation/beach.jpg", mime="image/jpeg", size=5000),
        BlobObject(path="photos/vacation/sunset.jpg", mime="image/jpeg", size=4800),
    ]


def test_walk_groups_by_folder_not_one_entity_per_object(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    slugs = walk_blob_source(_mixed_objects(), source_id="dropbox", store=store)
    assert len(slugs) == 2  # 2 immediate folders, not 5 objects


def test_folder_entity_carries_file_count_and_predominant_type(
    workspace: Path, db: MemoryDB
) -> None:
    store = _store(workspace, db)
    walk_blob_source(_mixed_objects(), source_id="dropbox", store=store)

    reports_slug = next(s for s in store.slugs() if "reports" in s)
    entity = store.read(reports_slug)
    assert entity is not None
    predicates = {f.predicate: f.value for f in entity.facts}
    assert predicates["file_count"] == "3"
    assert predicates["predominant_type"] == "application/pdf"


def test_folder_entity_carries_dominating_classification(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    objects = [
        BlobObject(path="secure/a.pdf", mime="application/pdf", classification="unclassified"),
        BlobObject(path="secure/b.pdf", mime="application/pdf", classification="secret"),
    ]
    walk_blob_source(objects, source_id="dropbox", store=store)

    secure_slug = next(s for s in store.slugs() if "secure" in s)
    entity = store.read(secure_slug)
    assert entity is not None
    predicates = {f.predicate: f.value for f in entity.facts}
    assert predicates["classification"] == "secret"  # raised, not the lower unclassified


def test_rewalk_same_objects_does_not_duplicate_folder_entities(
    workspace: Path, db: MemoryDB
) -> None:
    store = _store(workspace, db)
    first = walk_blob_source(_mixed_objects(), source_id="dropbox", store=store)
    second = walk_blob_source(_mixed_objects(), source_id="dropbox", store=store)

    assert set(first) == set(second)
    assert len(store.slugs()) == 2  # still exactly 2, no duplicate folder entities


def test_folder_slugs_are_written_as_entity_files_for_tagging(
    workspace: Path, db: MemoryDB
) -> None:
    store = _store(workspace, db)
    slugs = walk_blob_source(_mixed_objects(), source_id="dropbox", store=store)

    assert slugs, "expected at least one folder entity slug"
    for slug in slugs:
        assert store.path_for(slug).exists()
        assert slug.startswith("blob-dropbox-")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
