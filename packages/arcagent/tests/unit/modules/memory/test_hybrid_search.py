"""Tests for HybridSearch — BM25 + vector search across memory tiers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.config import MemoryConfig
from arcagent.modules.memory.hybrid_search import HybridSearch, SearchResult


def _make_search(workspace: Path) -> HybridSearch:
    return HybridSearch(workspace=workspace, config=MemoryConfig())


class TestSQLiteSchemaCreation:
    """T4.2.1: FTS5, metadata tables created."""

    @pytest.mark.asyncio()
    async def test_db_created_on_first_use(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        conn = search._ensure_db()
        assert conn is not None
        db_path = tmp_path / "search.db"
        assert db_path.exists()
        await search.close()

    @pytest.mark.asyncio()
    async def test_fts5_table_exists(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        conn = search._ensure_db()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_chunks'"
        )
        assert cursor.fetchone() is not None
        await search.close()

    @pytest.mark.asyncio()
    async def test_metadata_table_exists(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        conn = search._ensure_db()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='search_meta'"
        )
        assert cursor.fetchone() is not None
        await search.close()

    @pytest.mark.asyncio()
    async def test_indexed_files_table_exists(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        conn = search._ensure_db()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='indexed_files'"
        )
        assert cursor.fetchone() is not None
        await search.close()


class TestBM25Search:
    """T4.2.2: BM25 keyword matching and ranking."""

    @pytest.mark.asyncio()
    async def test_bm25_finds_matching_content(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        # Create notes with searchable content
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "2026-02-15.md").write_text("Project deadline is Friday\nNeed to review code")

        await search.reindex_if_needed()
        results = search._bm25_search("deadline Friday", top_k=5)
        assert len(results) >= 1
        assert any("deadline" in r.content.lower() for r in results)
        await search.close()

    @pytest.mark.asyncio()
    async def test_bm25_no_results_for_missing_term(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "2026-02-15.md").write_text("Meeting with team about architecture")

        await search.reindex_if_needed()
        results = search._bm25_search("xyznonexistent", top_k=5)
        assert len(results) == 0
        await search.close()


class TestScopeFiltering:
    """T4.2.4a: Scope parameter limits search."""

    @pytest.mark.asyncio()
    async def test_scope_notes_only(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "2026-02-15.md").write_text("Important meeting notes")
        context_file = tmp_path / "context.md"
        context_file.write_text("Important context data")

        await search.reindex_if_needed()
        results = await search.search("important", top_k=10, scope="notes")
        # Should only return notes results
        for r in results:
            assert "notes/" in r.source
        await search.close()


class TestLazyReindex:
    """T4.2.5: Only rebuild when files changed."""

    @pytest.mark.asyncio()
    async def test_no_reindex_when_unchanged(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "test.md").write_text("test content")

        await search.reindex_if_needed()
        initial_count = search._index_count

        # No changes — should not reindex
        await search.reindex_if_needed()
        assert search._index_count == initial_count
        await search.close()

    @pytest.mark.asyncio()
    async def test_reindex_on_new_file(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "test.md").write_text("original content")

        await search.reindex_if_needed()
        initial_count = search._index_count

        # Add new file
        (notes_dir / "new.md").write_text("new content here")
        await search.reindex_if_needed()
        assert search._index_count > initial_count
        await search.close()


class TestDocumentChunking:
    """T4.2.6: ~400 tokens, heading boundaries."""

    def test_short_document_single_chunk(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        chunks = search._chunk_document("Short content", "notes/test.md")
        assert len(chunks) == 1
        assert chunks[0].content == "Short content"

    def test_long_document_multiple_chunks(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        # Create content longer than 400 tokens (~1600 chars)
        long_content = "\n".join([f"Line {i}: " + "x" * 80 for i in range(30)])
        chunks = search._chunk_document(long_content, "notes/test.md")
        assert len(chunks) > 1

    def test_heading_boundary_respected(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        content = "# Section 1\n" + "content " * 200 + "\n# Section 2\n" + "more " * 200
        chunks = search._chunk_document(content, "notes/test.md")
        # Should prefer splitting at headings
        assert len(chunks) >= 2


class TestSqliteVecFallback:
    """T4.2.8: BM25-only when sqlite-vec unavailable."""

    @pytest.mark.asyncio()
    async def test_search_works_without_vec(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        search._vec_available = False
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "test.md").write_text("test content for search")

        await search.reindex_if_needed()
        results = await search.search("test content", top_k=5)
        # Should work with BM25 only
        assert isinstance(results, list)
        await search.close()


class TestRebuild:
    """T4.2.9: Full reindex from scratch."""

    @pytest.mark.asyncio()
    async def test_rebuild_clears_and_reindexes(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "test.md").write_text("original data")

        await search.reindex_if_needed()
        (notes_dir / "test.md").write_text("updated data")
        await search.rebuild()

        results = search._bm25_search("updated", top_k=5)
        assert len(results) >= 1
        await search.close()


class TestWALMode:
    """T4.2.10: WAL mode for concurrent reads."""

    @pytest.mark.asyncio()
    async def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        conn = search._ensure_db()
        cursor = conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode == "wal"
        await search.close()


class TestSearchResult:
    """Verify SearchResult dataclass."""

    def test_search_result_fields(self) -> None:
        result = SearchResult(
            source="notes/2026-02-15.md",
            content="Meeting notes",
            score=0.85,
            match_type="bm25",
        )
        assert result.source == "notes/2026-02-15.md"
        assert result.score == 0.85
        assert result.match_type == "bm25"
