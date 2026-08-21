"""Blob ontology discovery walker (SPEC-073 COMP-009).

A coarse folder/type catalog written as Entity+Fact — ``O(folders)`` writes,
never ``O(objects)``. Per-file detail belongs in chunks (COMP-006); this is
only the discovery-time map an agent uses to orient inside a blob source
(Dropbox, S3, a file share, ...) before it goes looking for anything.
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel

from arcmemory.security import dominating_classification
from arcmemory.slug import canonical_slug
from arcmemory.stores.semantic import SemanticStore


class BlobObject(BaseModel):
    """One object surfaced by a blob-storage source."""

    path: str
    mime: str = ""
    size: int = 0
    classification: str = "unclassified"


def _folder_of(path: str) -> str:
    """Immediate parent folder of a blob path (``""`` for a top-level object)."""
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _predominant_type(objects: list[BlobObject]) -> str:
    """Most common mime (extension fallback) among a folder's objects."""
    types = [obj.mime or obj.path.rsplit(".", 1)[-1] for obj in objects]
    return Counter(types).most_common(1)[0][0]


def walk_blob_source(
    objects: list[BlobObject],
    *,
    source_id: str,
    store: SemanticStore,
    now: str = "",
) -> list[str]:
    """Group ``objects`` by immediate folder and write one ``blob_folder`` Entity each.

    Grouping is a single ``O(objects)`` pass (unavoidable — every object must be
    looked at once); the store writes that follow are ``O(folders)``, never one
    per object. Each folder gets a deterministic slug
    (``blob-<source_id>-<folder-slug>``) so re-walking the same objects updates
    the SAME entity per folder via ``write_fact``'s ``was:`` trail instead of
    minting a duplicate — folder names become entity files, which is what makes
    them tagging cues for free.
    """
    groups: dict[str, list[BlobObject]] = {}
    for obj in objects:
        groups.setdefault(_folder_of(obj.path), []).append(obj)

    slugs: list[str] = []
    for folder, folder_objects in groups.items():
        folder_label = folder or "root"
        slug = canonical_slug(f"blob-{source_id}-{folder_label}")
        classification = dominating_classification(
            [obj.classification for obj in folder_objects]
        )
        for predicate, value in (
            ("path", folder_label),
            ("file_count", str(len(folder_objects))),
            ("predominant_type", _predominant_type(folder_objects)),
            ("classification", classification),
            ("last_synced", now),
        ):
            store.write_fact(
                slug,
                predicate,
                value,
                name=folder_label,
                entity_type="blob_folder",
                classification=classification,
            )
        slugs.append(slug)

    return slugs


__all__ = ["BlobObject", "walk_blob_source"]
