"""Owning service for collection ``index.md`` files.

Only this module writes collection indexes.  Readers must call ``verify`` and
fail closed when an operator or another process edits the reserved artifact.
The memory service owns ``workspace/memory/index.md``; the CLI's separate
``workspace/index.md`` is a scaffolded workspace collection and is not part of
memory retrieval.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from arcokf import (
    CollectionEntry,
    candidate_documents,
    document_entry,
    inventory_documents,
    render_collection_index,
    validate_collection_index,
)

from arcmemory.mdfile import atomic_write_text

#: Top-level memory subtrees that are collections of their own. Every connected
#: source keeps ``memory/connected/<source_id>/index.md`` and its own document
#: pool. Walking them from ``memory/index.md`` re-read and re-hashed every
#: connected document on each memory verification — tens of thousands of files
#: on every recall pass — and copied connected titles into the recall scope.
MEMORY_NESTED_COLLECTIONS = frozenset({"connected"})

#: Filesystem timestamps come from a coarse kernel clock that can trail
#: ``time.time_ns``. Documents newer than the watermark minus this slack are
#: looked at again on the next refresh; re-reading a few is harmless, missing
#: one is not.
_MTIME_SLACK_NS = 2_000_000_000


class CollectionIndexStore:
    """Synchronize one collection's reserved ``index.md`` from its documents."""

    def __init__(
        self,
        collection_root: Path,
        *,
        index_name: str = "index.md",
        nested_collections: frozenset[str] = frozenset(),
    ) -> None:
        self._root = Path(collection_root)
        self._index = self._root / index_name
        self._nested = nested_collections

    @property
    def index_path(self) -> Path:
        """Return the reserved index path owned by this store."""
        return self._index

    def sync(self) -> int:
        """Atomically create/update the index from the current valid inventory."""
        entries = inventory_documents(self._root, nested_collections=self._nested)
        rendered = render_collection_index(entries)
        try:
            current = self._index.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            current = ""
        if current != rendered:
            atomic_write_text(self._index, rendered)
        return len(entries)

    def refresh(self) -> int:
        """Apply only the documents changed since the last index write.

        Every present document is ``stat``-ed; only those modified since the
        index was last written are read and hashed, and entries whose file is
        gone are dropped. The index mtime is then set to a watermark taken
        before the walk, so a document written during it is picked up next
        time. An index that is missing or not canonical is rebuilt with ``sync``.
        """
        validation = validate_collection_index(self._index)
        if not validation.valid or self._has_nested(validation.entries):
            return self.sync()
        watermark = time.time_ns() - _MTIME_SLACK_NS
        since = self._index.stat().st_mtime_ns
        entries = {entry.path: entry for entry in validation.entries}
        root = self._root.resolve()
        present: set[str] = set()
        for path in candidate_documents(root, nested_collections=self._nested):
            relative = path.relative_to(root).as_posix()
            present.add(relative)
            if path.stat().st_mtime_ns < since:
                continue
            entry = document_entry(path, root)
            if entry is None:
                entries.pop(relative, None)
            else:
                entries[relative] = entry
        kept = [entry for path, entry in entries.items() if path in present]
        self._write_if_changed(kept, validation.entries)
        os.utime(self._index, ns=(watermark, watermark))
        return len(kept)

    def upsert_document(self, document: Path) -> int:
        """Update one entry from a committed document without walking siblings."""
        validation = validate_collection_index(self._index)
        if not validation.valid or self._has_nested(validation.entries):
            return self.sync()
        current = validation.entries
        relative = document.resolve().relative_to(self._root.resolve()).as_posix()
        if relative.split("/", 1)[0] in self._nested:
            return len(current)
        entries = [entry for entry in current if entry.path != relative]
        entry = document_entry(document, self._root)
        if entry is not None:
            entries.append(entry)
        rendered = render_collection_index(entries)
        if self._index.read_text(encoding="utf-8") != rendered:
            atomic_write_text(self._index, rendered)
        return len(entries)

    def remove_document(self, document: Path) -> int:
        """Remove one deleted document from a valid index without a tree walk."""
        validation = validate_collection_index(self._index)
        if not validation.valid or self._has_nested(validation.entries):
            return self.sync()
        relative = document.resolve().relative_to(self._root.resolve()).as_posix()
        current = validation.entries
        entries = [entry for entry in current if entry.path != relative]
        atomic_write_text(self._index, render_collection_index(entries))
        return len(entries)

    def verify(self) -> bool:
        """Return whether the on-disk index and every listed document are trusted."""
        return validate_collection_index(
            self._index, self._root, nested_collections=self._nested
        ).valid

    def _has_nested(self, entries: tuple[CollectionEntry, ...]) -> bool:
        """True when an index still lists documents of a nested collection.

        An index written before the nested subtree was excluded carries every
        connected document; rewriting it once from the real inventory is what
        shrinks it back to the collection it describes.
        """
        return any(entry.path.split("/", 1)[0] in self._nested for entry in entries)

    def _write_if_changed(
        self, entries: list[CollectionEntry], current: tuple[CollectionEntry, ...]
    ) -> None:
        if sorted(entries, key=lambda entry: entry.path) != list(current):
            atomic_write_text(self._index, render_collection_index(entries))


def memory_collection(mem_dir: Path) -> CollectionIndexStore:
    """The owning store for ``memory/index.md``, excluding nested collections."""
    return CollectionIndexStore(mem_dir, nested_collections=MEMORY_NESTED_COLLECTIONS)


def routing_text(index_text: str) -> str:
    """The human routing lines of a collection index, without machine comments.

    The entry comments stay on disk for verification; only the heading and the
    link lines are worth searching.
    """
    return "\n".join(line for line in index_text.splitlines() if line.startswith(("# ", "- [")))


def refresh_memory_document(path: Path) -> None:
    """Refresh one memory entry after an owning markdown write.

    Memory stores call this after their durable write, keeping index ownership
    at the collection seam without making the generic writer scan the tree.
    """
    for parent in (path, *path.parents):
        if parent.name == "memory":
            memory_collection(parent).upsert_document(path)
            return


__all__ = [
    "MEMORY_NESTED_COLLECTIONS",
    "CollectionIndexStore",
    "memory_collection",
    "refresh_memory_document",
    "routing_text",
]
