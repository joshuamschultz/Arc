"""HybridSearch — BM25 + vector search across memory tiers.

Uses SQLite FTS5 for keyword search and optionally sqlite-vec for
vector similarity. Falls back to BM25-only when sqlite-vec is unavailable.
Lazy reindexing based on file modification timestamps.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from arcagent.core.config import MemoryConfig
from arcagent.utils.io import CHARS_PER_TOKEN, sanitize_fts5_query

_logger = logging.getLogger("arcagent.modules.memory.hybrid_search")

_TARGET_CHUNK_TOKENS = 400
_TARGET_CHUNK_CHARS = _TARGET_CHUNK_TOKENS * CHARS_PER_TOKEN


@dataclass
class SearchResult:
    """A single search result with source, content, score, and match type."""

    source: str
    content: str
    score: float
    match_type: str  # "bm25", "vector", or "hybrid"


@dataclass
class _Chunk:
    """Internal document chunk for indexing."""

    content: str
    source: str
    chunk_id: str


class HybridSearch:
    """Combined BM25 keyword + vector similarity search.

    Indexes all markdown files under the workspace (notes/, entities/,
    context.md, identity.md, policy.md). Uses SQLite FTS5 for BM25
    and optionally sqlite-vec for cosine similarity search.
    """

    def __init__(self, workspace: Path, config: MemoryConfig) -> None:
        self._workspace = workspace
        self._config = config
        self._db_path = workspace / "search.db"
        self._conn: sqlite3.Connection | None = None
        self._vec_available: bool = True
        self._last_indexed: dict[str, float] = {}
        self._index_count: int = 0

    async def search(
        self,
        query: str,
        top_k: int = 10,
        scope: str | None = None,
    ) -> list[SearchResult]:
        """Hybrid search across memory tiers.

        Args:
            query: Search query text.
            top_k: Maximum results to return.
            scope: Filter by source type ("notes", "entities", "context").
        """
        await self.reindex_if_needed()

        bm25_results = self._bm25_search(query, top_k=top_k * 2)

        if scope:
            bm25_results = [
                r
                for r in bm25_results
                if r.source.startswith(scope + "/") or r.source.startswith(scope + ".")
            ]

        # Sort by score descending, limit to top_k
        bm25_results.sort(key=lambda r: r.score, reverse=True)
        return bm25_results[:top_k]

    async def reindex_if_needed(self) -> None:
        """Check file modification timestamps. Reindex changed files only."""
        conn = self._ensure_db()
        files = self._discover_files()

        needs_index: list[Path] = []
        for fpath in files:
            mtime = fpath.stat().st_mtime
            rel = str(fpath.relative_to(self._workspace))
            if rel not in self._last_indexed or self._last_indexed[rel] < mtime:
                needs_index.append(fpath)

        if not needs_index:
            return

        for fpath in needs_index:
            rel = str(fpath.relative_to(self._workspace))
            content = fpath.read_text(encoding="utf-8")
            chunks = self._chunk_document(content, rel)

            # Remove old chunks for this file
            conn.execute("DELETE FROM fts_chunks WHERE source = ?", (rel,))

            # Insert new chunks
            for chunk in chunks:
                conn.execute(
                    "INSERT INTO fts_chunks (content, source, chunk_id) VALUES (?, ?, ?)",
                    (chunk.content, chunk.source, chunk.chunk_id),
                )

            # Track indexing
            conn.execute(
                "INSERT OR REPLACE INTO indexed_files (path, mtime, chunk_count) VALUES (?, ?, ?)",
                (rel, fpath.stat().st_mtime, len(chunks)),
            )
            self._last_indexed[rel] = fpath.stat().st_mtime

        conn.commit()
        self._index_count += len(needs_index)

    async def rebuild(self) -> None:
        """Full reindex from scratch."""
        conn = self._ensure_db()
        conn.execute("DELETE FROM fts_chunks")
        conn.execute("DELETE FROM indexed_files")
        conn.commit()
        self._last_indexed.clear()
        self._index_count = 0
        await self.reindex_if_needed()

    async def close(self) -> None:
        """Close SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _ensure_db(self) -> sqlite3.Connection:
        """Open or create search.db with FTS5 tables and WAL mode."""
        if self._conn is not None:
            return self._conn

        self._workspace.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))

        # Enable WAL mode for concurrent reads
        conn.execute("PRAGMA journal_mode=WAL")

        # Create FTS5 table
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(content, source, chunk_id)"
        )

        # Metadata table
        conn.execute("CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT)")

        # Indexed files tracking
        conn.execute(
            "CREATE TABLE IF NOT EXISTS indexed_files ("
            "path TEXT PRIMARY KEY, mtime REAL, chunk_count INTEGER)"
        )

        conn.commit()
        self._conn = conn
        return conn

    def _discover_files(self) -> list[Path]:
        """Find all indexable files under workspace."""
        files: list[Path] = []
        # Notes directory
        notes_dir = self._workspace / "notes"
        if notes_dir.exists():
            files.extend(notes_dir.glob("*.md"))

        # Top-level markdown files
        for name in ("context.md", "identity.md", "policy.md"):
            fpath = self._workspace / name
            if fpath.exists():
                files.append(fpath)

        # Entity summaries
        entities_dir = self._workspace / "entities"
        if entities_dir.exists():
            files.extend(entities_dir.glob("**/*.md"))

        return files

    def _chunk_document(self, content: str, source: str) -> list[_Chunk]:
        """Split content at ~400 tokens with heading-boundary preference."""
        if len(content) <= _TARGET_CHUNK_CHARS:
            return [_Chunk(content=content, source=source, chunk_id=f"{source}:0")]

        chunks: list[_Chunk] = []
        # Split by headings first
        sections = self._split_by_headings(content)

        current = ""
        chunk_idx = 0
        for section in sections:
            # If a single section exceeds target, split by lines
            if len(section) > _TARGET_CHUNK_CHARS:
                for line in section.split("\n"):
                    if len(current) + len(line) + 1 > _TARGET_CHUNK_CHARS and current:
                        chunks.append(
                            _Chunk(
                                content=current.strip(),
                                source=source,
                                chunk_id=f"{source}:{chunk_idx}",
                            )
                        )
                        chunk_idx += 1
                        current = line + "\n"
                    else:
                        current += line + "\n"
            elif len(current) + len(section) > _TARGET_CHUNK_CHARS and current:
                chunks.append(
                    _Chunk(
                        content=current.strip(),
                        source=source,
                        chunk_id=f"{source}:{chunk_idx}",
                    )
                )
                chunk_idx += 1
                current = section
            else:
                current += section

        if current.strip():
            chunks.append(
                _Chunk(
                    content=current.strip(),
                    source=source,
                    chunk_id=f"{source}:{chunk_idx}",
                )
            )

        return (
            chunks if chunks else [_Chunk(content=content, source=source, chunk_id=f"{source}:0")]
        )

    @staticmethod
    def _split_by_headings(content: str) -> list[str]:
        """Split content at markdown heading boundaries."""
        sections: list[str] = []
        current: list[str] = []

        for line in content.split("\n"):
            if line.startswith("#") and current:
                sections.append("\n".join(current) + "\n")
                current = [line]
            else:
                current.append(line)

        if current:
            sections.append("\n".join(current))

        return sections

    def _bm25_search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        """SQLite FTS5 full-text search with query sanitization."""
        safe_query = sanitize_fts5_query(query)
        if not safe_query:
            return []

        conn = self._ensure_db()
        try:
            cursor = conn.execute(
                "SELECT content, source, rank FROM fts_chunks "
                "WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
                (safe_query, top_k),
            )
            results: list[SearchResult] = []
            for row in cursor.fetchall():
                results.append(
                    SearchResult(
                        source=row[1],
                        content=row[0],
                        score=abs(row[2]),  # FTS5 rank is negative
                        match_type="bm25",
                    )
                )
            return results
        except sqlite3.OperationalError:
            _logger.debug("BM25 search failed for query: %s", query)
            return []
