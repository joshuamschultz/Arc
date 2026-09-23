from __future__ import annotations

import os
from pathlib import Path

import pytest

import arcmemory.collection_index as collection_index
from arcmemory.collection_index import CollectionIndexStore


def _doc(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: Entity\nclassification: unclassified\ntitle: {title}\n---\n{title}\n",
        encoding="utf-8",
    )


def test_store_updates_index_atomically_and_excludes_operational_files(tmp_path: Path) -> None:
    _doc(tmp_path / "entities" / "alpha.md", "Alpha")
    _doc(tmp_path / "entities" / "beta.md", "Beta")
    (tmp_path / "config.toml").write_text("secret = true", encoding="utf-8")

    store = CollectionIndexStore(tmp_path)
    assert store.sync() == 2
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "entities/alpha.md" in index
    assert "entities/beta.md" in index
    assert "config.toml" not in index

    (tmp_path / "entities" / "beta.md").unlink()
    assert store.sync() == 1
    assert "entities/beta.md" not in (tmp_path / "index.md").read_text(encoding="utf-8")


def test_tampered_index_is_not_trusted(tmp_path: Path) -> None:
    _doc(tmp_path / "entities" / "alpha.md", "Alpha")
    store = CollectionIndexStore(tmp_path)
    store.sync()
    index = tmp_path / "index.md"
    index.write_text(index.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    assert not store.verify()


def test_incremental_updates_do_not_walk_the_collection_per_document(
    tmp_path: Path, monkeypatch
) -> None:
    store = CollectionIndexStore(tmp_path)
    first = tmp_path / "first.md"
    _doc(first, "First")
    store.sync()
    calls = 0
    original = collection_index.inventory_documents

    def counted(root: Path):
        nonlocal calls
        calls += 1
        return original(root)

    monkeypatch.setattr(collection_index, "inventory_documents", counted)
    for number in range(10):
        path = tmp_path / f"doc-{number}.md"
        _doc(path, f"Doc {number}")
        store.upsert_document(path)

    assert calls == 0
    assert store.verify()


def _age(path: Path, seconds: float = 3600.0) -> None:
    """Backdate a file so only documents written after it count as changed."""
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))


def test_refresh_reads_only_documents_changed_since_the_last_index_write(
    tmp_path: Path, monkeypatch
) -> None:
    for number in range(20):
        _doc(tmp_path / f"doc-{number:02d}.md", f"Doc {number}")
        _age(tmp_path / f"doc-{number:02d}.md")
    store = CollectionIndexStore(tmp_path)
    store.sync()
    _doc(tmp_path / "doc-03.md", "Doc three rewritten")
    _doc(tmp_path / "new.md", "Brand new")
    (tmp_path / "doc-07.md").unlink()
    read: list[str] = []
    original = collection_index.document_entry

    def counted(path: Path, root: Path):  # type: ignore[no-untyped-def]
        read.append(path.name)
        return original(path, root)

    monkeypatch.setattr(collection_index, "document_entry", counted)
    monkeypatch.setattr(
        collection_index,
        "inventory_documents",
        lambda *a, **k: pytest.fail("refresh must not walk and hash the whole collection"),
    )

    assert store.refresh() == 20

    assert sorted(read) == ["doc-03.md", "new.md"]
    monkeypatch.undo()
    assert store.verify()
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "Doc three rewritten" in index and "doc-07.md" not in index


def test_refresh_rebuilds_an_index_that_is_not_canonical(tmp_path: Path) -> None:
    _doc(tmp_path / "alpha.md", "Alpha")
    store = CollectionIndexStore(tmp_path)
    store.sync()
    index = tmp_path / "index.md"
    index.write_text(index.read_text(encoding="utf-8") + "- [Injected](evil.md)\n", "utf-8")

    store.refresh()

    assert store.verify()
    assert "Injected" not in index.read_text(encoding="utf-8")


def test_memory_index_leaves_connected_sources_to_their_own_index(tmp_path: Path) -> None:
    """``memory/index.md`` once listed every connected document (29 MB on a live box).

    Each source already routes through ``memory/connected/<id>/index.md``. The
    first memory write after the fix rewrites the oversized legacy index from
    the memory inventory alone.
    """
    mem = tmp_path / "memory"
    _doc(mem / "entities" / "alpha.md", "Alpha")
    _doc(mem / "connected" / "source" / "beta.md", "Beta")
    CollectionIndexStore(mem).sync()  # the legacy, un-nested index
    assert "connected/source/beta.md" in (mem / "index.md").read_text(encoding="utf-8")

    new_card = mem / "entities" / "gamma.md"
    _doc(new_card, "Gamma")
    collection_index.refresh_memory_document(new_card)

    rewritten = (mem / "index.md").read_text(encoding="utf-8")
    assert "connected/" not in rewritten
    assert "entities/gamma.md" in rewritten
    assert collection_index.memory_collection(mem).verify()
