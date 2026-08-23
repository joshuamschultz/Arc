"""Owning service for collection ``index.md`` files.

Only this module writes collection indexes.  Readers must call ``verify`` and
fail closed when an operator or another process edits the reserved artifact.
The memory service owns ``workspace/memory/index.md``; the CLI's separate
``workspace/index.md`` is a scaffolded workspace collection and is not part of
memory retrieval.
"""

from __future__ import annotations

from pathlib import Path

from arcokf import (
    document_entry,
    inventory_documents,
    render_collection_index,
    validate_collection_index,
)

from arcmemory.mdfile import atomic_write_text


class CollectionIndexStore:
    """Synchronize one collection's reserved ``index.md`` from its documents."""

    def __init__(self, collection_root: Path, *, index_name: str = "index.md") -> None:
        self._root = Path(collection_root)
        self._index = self._root / index_name

    @property
    def index_path(self) -> Path:
        """Return the reserved index path owned by this store."""
        return self._index

    def sync(self) -> int:
        """Atomically create/update the index from the current valid inventory."""
        entries = inventory_documents(self._root)
        rendered = render_collection_index(entries)
        try:
            current = self._index.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            current = ""
        if current != rendered:
            atomic_write_text(self._index, rendered)
        return len(entries)

    def upsert_document(self, document: Path) -> int:
        """Update one entry from a committed document without walking siblings."""
        validation = validate_collection_index(self._index)
        if not validation.valid:
            return self.sync()
        current = validation.entries
        relative = document.resolve().relative_to(self._root.resolve()).as_posix()
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
        if not validation.valid:
            return self.sync()
        relative = document.resolve().relative_to(self._root.resolve()).as_posix()
        current = validation.entries
        entries = [entry for entry in current if entry.path != relative]
        atomic_write_text(self._index, render_collection_index(entries))
        return len(entries)

    def verify(self) -> bool:
        """Return whether the on-disk index and every listed document are trusted."""
        return validate_collection_index(self._index, self._root).valid


def refresh_memory_document(path: Path) -> None:
    """Refresh one memory entry after an owning markdown write.

    Memory stores call this after their durable write, keeping index ownership
    at the collection seam without making the generic writer scan the tree.
    """
    for parent in (path, *path.parents):
        if parent.name == "memory":
            CollectionIndexStore(parent).upsert_document(path)
            return


__all__ = ["CollectionIndexStore", "refresh_memory_document"]
