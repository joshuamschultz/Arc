"""Tests for HybridSearch — BM25 + vector search across memory tiers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from arcagent.modules.memory.config import MemoryConfig
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


class TestEmptyQuery:
    """Test search with empty query."""

    async def test_search_with_empty_query(self, tmp_path: Path) -> None:
        """Empty query returns no results."""
        search = _make_search(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "test.md").write_text("some content")

        await search.reindex_if_needed()
        results = await search.search("", top_k=10)
        # Empty query should sanitize to empty and return no results
        assert len(results) == 0
        await search.close()


class TestReindexWithNoFiles:
    """Test reindex when no files exist."""

    async def test_reindex_with_no_files(self, tmp_path: Path) -> None:
        """Reindex when no markdown files exist should complete without error."""
        search = _make_search(tmp_path)
        # No notes, context, identity, or policy files
        await search.reindex_if_needed()
        # Should complete without error
        assert search._index_count == 0
        await search.close()


class TestCloseMethod:
    """Test close() method."""

    async def test_close_closes_connection(self, tmp_path: Path) -> None:
        """close() closes the SQLite connection."""
        search = _make_search(tmp_path)
        conn = search._ensure_db()
        assert conn is not None
        assert search._conn is not None

        await search.close()
        assert search._conn is None


class TestSearchResultRanking:
    """Test search result ranking and scoring."""

    async def test_search_results_sorted_by_score(self, tmp_path: Path) -> None:
        """Search results are sorted by score descending."""
        search = _make_search(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "note1.md").write_text("important meeting about project deadline")
        (notes_dir / "note2.md").write_text("meeting notes")
        (notes_dir / "note3.md").write_text("important project deadline discussion")

        await search.reindex_if_needed()
        results = await search.search("important project deadline", top_k=10)

        # Results should be sorted by score
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

        await search.close()


class TestBM25QueryFailure:
    """Test BM25 search operational error handling."""

    async def test_bm25_search_handles_operational_error(self, tmp_path: Path) -> None:
        """BM25 search returns empty list on OperationalError."""
        search = _make_search(tmp_path)
        conn = search._ensure_db()

        # Force an operational error by using a malformed query that gets through sanitization
        # but fails at SQLite level (unlikely but covered for safety)
        results = search._bm25_search("*" * 1000, top_k=10)
        # Should return empty list, not raise
        assert results == []

        await search.close()


class TestChunkingEdgeCases:
    """Test document chunking edge cases."""

    def test_chunk_single_very_long_section(self, tmp_path: Path) -> None:
        """A single section exceeding target chunk size splits by lines."""
        search = _make_search(tmp_path)
        # Single section with no headings, very long
        long_line = "x" * 2000
        content = "\n".join([long_line for _ in range(10)])
        chunks = search._chunk_document(content, "notes/test.md")
        # Should split into multiple chunks
        assert len(chunks) > 1

    def test_chunk_with_empty_sections(self, tmp_path: Path) -> None:
        """Empty sections are handled gracefully."""
        search = _make_search(tmp_path)
        content = "# Section 1\n\n# Section 2\n\n# Section 3"
        chunks = search._chunk_document(content, "notes/test.md")
        # Should produce at least one chunk
        assert len(chunks) >= 1

    def test_split_by_headings_preserves_structure(self, tmp_path: Path) -> None:
        """_split_by_headings maintains heading structure."""
        search = _make_search(tmp_path)
        content = "# Heading 1\nContent 1\n# Heading 2\nContent 2"
        sections = search._split_by_headings(content)
        assert len(sections) >= 2
        assert any("Heading 1" in s for s in sections)
        assert any("Heading 2" in s for s in sections)


class TestEntityFiles:
    """Line 194: Entity directory glob (flat markdown files)."""

    def test_entities_included_in_file_list(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        entities_dir = tmp_path / "entities"
        entities_dir.mkdir(parents=True)
        (entities_dir / "josh.md").write_text(
            "---\nname: Josh\ntype: person\n---\nJosh is a developer"
        )
        files = search._discover_files()
        assert any("josh.md" in str(f) for f in files)


class TestChunkLargeSections:
    """Lines 226-234: Chunking when sections exceed target size."""

    def test_section_exceeds_target_produces_multiple_chunks(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        # Create content with a single very large section (>1600 chars)
        big_section = "# Big\n" + ("Line of content here. " * 50 + "\n") * 40
        chunks = search._chunk_document(big_section, "notes/big.md")
        assert len(chunks) > 1

    def test_accumulated_sections_split(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        # Multiple moderate sections that accumulate past target
        content = ""
        for i in range(20):
            content += f"# Section {i}\n" + "Content here. " * 30 + "\n\n"
        chunks = search._chunk_document(content, "notes/multi.md")
        assert len(chunks) > 1


class TestBm25SearchFailure:
    """Lines 293-295: OperationalError returns empty list."""

    def test_bm25_operational_error_via_mock(self, tmp_path: Path) -> None:
        search = _make_search(tmp_path)
        search._ensure_db()
        # Replace connection with mock that raises OperationalError
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("test")
        search._conn = mock_conn
        results = search._bm25_search("query", top_k=5)
        assert results == []
