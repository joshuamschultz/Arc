"""Alpha-2 P4 — the surface index embeds human knowledge only.

Frontmatter machine fields (``last_updated``, ``links_to``, ``entity_type``,
hashes) and connector bookkeeping cards (``mapping-*``, ``source-*``, blob
folders, db tables) must never reach the embedder or the FTS table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.chunk import RecursiveChunker
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import IndexRebuilder
from arcmemory.index.source import (
    BOOKKEEPING_ENTITY_TYPES,
    EMBED_TEXT_MAX_CHARS,
    MAX_CHUNK_BYTES,
    SourceChunk,
    bounded_chunks,
    embed_text,
    iter_source_chunks,
)
from arcmemory.index.surface import SurfaceIndex
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Scope


def _chunks(workspace: Path) -> list[SourceChunk]:
    return list(iter_source_chunks(workspace / "memory", workspace, []))


def _cards(workspace: Path) -> list[SourceChunk]:
    """Card chunks only (the collection ``index.md`` routing chunk is separate)."""
    return [c for c in _chunks(workspace) if "/entities/" in c.source_path]


def _semantic(workspace: Path, db: MemoryDB, scope: Scope) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db), scope=scope.key)


def test_frontmatter_machine_fields_are_stripped(workspace: Path, db: MemoryDB, scope: Scope):
    store = _semantic(workspace, db, scope)
    store.write_fact("alice", "colleague", "[[bob]]", name="Alice", entity_type="person")

    texts = [c.text for c in _cards(workspace)]

    assert len(texts) == 1
    text = texts[0]
    assert "Alice" in text and "colleague" in text
    for machine in ("last_updated", "links_to", "entity_type", "sha256:"):
        assert machine not in text


@pytest.mark.parametrize("entity_type", sorted(BOOKKEEPING_ENTITY_TYPES))
def test_bookkeeping_entity_types_yield_no_chunks(
    workspace: Path, db: MemoryDB, scope: Scope, entity_type: str
):
    store = _semantic(workspace, db, scope)
    store.write_fact(f"{entity_type}-x", "content_hash", "sha256:abc", entity_type=entity_type)

    assert _cards(workspace) == []
    routing = " ".join(c.text for c in _chunks(workspace))
    assert f"{entity_type}-x" not in routing, "routing lines must not advertise bookkeeping"
    assert "sha256:" not in routing


def test_bookkeeping_constant_covers_connector_cards():
    assert {"mapping", "source", "blob_folder", "db_table"} <= BOOKKEEPING_ENTITY_TYPES


def test_last_updated_only_change_keeps_the_chunk_text(
    workspace: Path, db: MemoryDB, scope: Scope
):
    store = _semantic(workspace, db, scope)
    store.write_fact("alice", "role", "engineer", name="Alice")
    path = workspace / "memory" / "entities" / "alice.md"
    before = [c.text for c in _cards(workspace)]

    lines = path.read_text(encoding="utf-8").split("\n")
    changed = [
        "last_updated: 1999-01-01" if line.startswith("last_updated:") else line for line in lines
    ]
    assert changed != lines, "fixture must actually alter last_updated"
    path.write_text("\n".join(changed), encoding="utf-8")
    after = [c.text for c in _cards(workspace)]

    assert before == after


async def test_rebuild_and_incremental_index_the_same_chunk_set(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
):
    store = _semantic(workspace, db, scope)
    store.write_fact("alice", "role", "engineer", name="Alice")
    store.write_fact("mapping-s1", "revision", "r1", entity_type="mapping")
    store.write_fact("source-s1", "kind", "dropbox", entity_type="source")

    await SurfaceIndex(db, workspace, scope, embedder=embedder).index_if_needed(embed=True)
    conn = db.connect()
    incremental = conn.execute(
        "SELECT chunk_id, text FROM fts_chunks ORDER BY chunk_id"
    ).fetchall()

    await IndexRebuilder(db, workspace, scope, config=MemoryConfig(), embedder=embedder).rebuild()
    rebuilt = conn.execute("SELECT chunk_id, text FROM fts_chunks ORDER BY chunk_id").fetchall()

    assert incremental == rebuilt
    assert [row[0] for row in rebuilt] == [
        "file:memory/entities/alice.md",
        "file:memory/index.md",
    ]
    joined = " ".join(row[1] for row in rebuilt)
    assert "mapping-s1" not in joined and "source-s1" not in joined


def test_one_huge_line_is_split_under_the_byte_cap():
    huge = "x" * 1_100_000

    chunks = list(bounded_chunks("file:a", "a", huge, "", 0.0))

    assert len(chunks) > 1
    assert all(len(c.text.encode("utf-8")) <= MAX_CHUNK_BYTES for c in chunks)


def test_recursive_chunker_splits_one_huge_line_under_the_byte_cap():
    huge = "word " * 300_000  # one >1 MB line, no newline

    chunks = RecursiveChunker().chunk(huge, source_path="doc")

    assert chunks
    assert all(len(c.text.encode("utf-8")) <= MAX_CHUNK_BYTES for c in chunks)


def test_embed_text_is_capped_at_the_model_window_while_fts_text_is_not():
    long_text = "a" * (EMBED_TEXT_MAX_CHARS * 3)

    assert len(embed_text(long_text)) == EMBED_TEXT_MAX_CHARS
    assert embed_text("short") == "short"
