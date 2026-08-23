from __future__ import annotations

from pathlib import Path

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
