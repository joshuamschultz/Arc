"""H-023 — MemoryOperator chunk browse/search: metadata, both search modes,
no-embedder degrade, and no-read-up gating on browse AND search.

Every fixture DB here is built through arcmemory's own capture path
(``ArcMemoryBrain.capture``) — never hand-inserted SQL — so these tests exercise
the same chunk rows production indexing writes (T-040/H-023).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from arcmemory.brain import ArcMemoryBrain
from arcmemory.index.rebuild import Embedder
from arcmemory.operator import (
    ChunkPage,
    ChunkSearchResult,
    MemoryOperator,
)

_DID = "did:arc:chunk-agent"
_VOCAB = ["alice", "bob"]

#: MemoryOperator builds its own MemoryDB at the default 384 dims internally (it
#: has no dims override), so the vector-mode tests need an embedder that matches
#: that width — conftest's ``StubEmbedder`` tops out at a 32-byte sha256 digest.
_OPERATOR_DIMS = 384


class _VectorEmbedder:
    """Deterministic 384-dim embedder matching MemoryOperator's default dims."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([digest[i % len(digest)] / 255.0 for i in range(_OPERATOR_DIMS)])
        return out


def _operator(workspace: Path, *, embedder: Embedder | None = None) -> MemoryOperator:
    return MemoryOperator(workspace, _DID, embedder=embedder, seed_vocabulary=_VOCAB)


async def _seed(workspace: Path, *, classification: str = "unclassified") -> None:
    brain = ArcMemoryBrain(workspace, _DID, seed_vocabulary=_VOCAB)
    await brain.capture(
        "alice shipped the widget release", kind="observation", classification=classification
    )
    await brain.capture(
        "bob reviewed the quarterly budget numbers", kind="observation", classification=classification
    )


# -- browse_chunks: metadata + newest-first + gate-before-pagination ----------


async def test_browse_chunks_returns_metadata(workspace: Path) -> None:
    await _seed(workspace)
    page = await _operator(workspace).browse_chunks(limit=10)

    assert isinstance(page, ChunkPage)
    assert page.total == 2
    assert len(page.items) == 2
    item = page.items[0]
    assert item.chunk_id
    assert item.source  # source_path
    assert item.scope == _DID
    assert item.classification == "unclassified"
    assert item.text  # hydrated chunk text
    assert item.score > 0.0


async def test_browse_chunks_paginates(workspace: Path) -> None:
    await _seed(workspace)
    op = _operator(workspace)
    first = await op.browse_chunks(limit=1, offset=0)
    second = await op.browse_chunks(limit=1, offset=1)

    assert first.total == 2 and second.total == 2
    assert len(first.items) == 1 and len(second.items) == 1
    assert first.items[0].chunk_id != second.items[0].chunk_id


async def test_browse_chunks_empty_workspace(workspace: Path) -> None:
    page = await _operator(workspace).browse_chunks()
    assert page.total == 0
    assert page.items == []


async def test_browse_chunks_caps_text_size(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID, seed_vocabulary=_VOCAB)
    long_text = "widget " * 400  # far past the ~500-char cap
    await brain.capture(long_text, kind="observation")

    page = await _operator(workspace).browse_chunks(limit=10)
    item = page.items[0]
    assert len(item.text) <= 500
    assert item.truncated is True


async def test_browse_chunks_short_text_not_truncated(workspace: Path) -> None:
    await _seed(workspace)
    page = await _operator(workspace).browse_chunks(limit=10)
    assert all(not item.truncated for item in page.items)


# -- search_chunks: literal (BM25) mode ---------------------------------------


async def test_search_chunks_literal_finds_match(workspace: Path) -> None:
    await _seed(workspace)
    result = await _operator(workspace).search_chunks("budget", mode="literal")

    assert isinstance(result, ChunkSearchResult)
    assert result.mode == "literal"
    assert result.degraded is False
    assert any("budget" in item.text for item in result.items)


async def test_search_chunks_literal_no_match_is_empty_not_degraded(workspace: Path) -> None:
    await _seed(workspace)
    result = await _operator(workspace).search_chunks("nonexistent-zzz-term", mode="literal")
    assert result.items == []
    assert result.degraded is False  # legitimately no matches, not a channel failure


# -- search_chunks: vector mode with a real embedder --------------------------


async def test_search_chunks_vector_finds_match_with_embedder(workspace: Path) -> None:
    await _seed(workspace)
    op = _operator(workspace, embedder=_VectorEmbedder())
    result = await op.search_chunks("widget release", mode="vector")

    assert result.mode == "vector"
    assert result.degraded is False
    assert result.items


# -- search_chunks: no embedder degrades LOUD, never raises -------------------


async def test_search_chunks_vector_degrades_loud_without_embedder(workspace: Path) -> None:
    await _seed(workspace)
    op = _operator(workspace, embedder=None)  # no embedder wired

    result = await op.search_chunks("budget", mode="vector")

    assert result.degraded is True
    assert result.mode == "literal"  # fell back, never raised
    assert any("budget" in item.text for item in result.items)  # literal still answers


# -- no-read-up gating on BOTH browse and search (REQ-060/061 reused) ---------


async def _seed_mixed_classification(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID, seed_vocabulary=_VOCAB)
    await brain.capture(
        "the widget shipping cadence is public", kind="observation", classification="unclassified"
    )
    await brain.capture(
        "the widget launch codes are secret", kind="observation", classification="SECRET"
    )


async def test_browse_chunks_excludes_over_clearance_chunk(workspace: Path) -> None:
    await _seed_mixed_classification(workspace)
    op = _operator(workspace)

    page = await op.browse_chunks(limit=10, clearance="unclassified")

    assert page.total == 1  # the SECRET chunk is gated out BEFORE pagination
    assert all("launch codes" not in item.text for item in page.items)
    assert all(item.classification != "SECRET" for item in page.items)


async def test_browse_chunks_keeps_dominating_clearance(workspace: Path) -> None:
    await _seed_mixed_classification(workspace)
    op = _operator(workspace)

    page = await op.browse_chunks(limit=10, clearance="SECRET")
    assert page.total == 2  # SECRET clearance dominates both


async def test_search_chunks_literal_excludes_over_clearance_chunk(workspace: Path) -> None:
    await _seed_mixed_classification(workspace)
    op = _operator(workspace)

    result = await op.search_chunks("widget", mode="literal", clearance="unclassified")

    assert all("launch codes" not in item.text for item in result.items)
    assert all(item.classification != "SECRET" for item in result.items)


async def test_search_chunks_vector_excludes_over_clearance_chunk(workspace: Path) -> None:
    await _seed_mixed_classification(workspace)
    op = _operator(workspace, embedder=_VectorEmbedder())

    result = await op.search_chunks("widget", mode="vector", clearance="unclassified")

    assert all("launch codes" not in item.text for item in result.items)
    assert all(item.classification != "SECRET" for item in result.items)
