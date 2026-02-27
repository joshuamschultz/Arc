"""BM25 search with adaptive wiki-link graph traversal.

Two-phase search:
1. BM25 scoring on entity corpus
2. Adaptive wiki-link traversal (BFS, BM25-scored hops)
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from rank_bm25 import BM25Plus  # type: ignore[import-untyped]

from arcteam.memory.classification import ClassificationChecker
from arcteam.memory.types import Classification, IndexEntry, SearchResult

if TYPE_CHECKING:
    from arcteam.memory.config import TeamMemoryConfig
    from arcteam.memory.index_manager import IndexManager
    from arcteam.memory.storage import MemoryStorage

logger = logging.getLogger(__name__)

# Regex patterns compiled once
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_HEADING_RE = re.compile(r"^#+\s+", re.MULTILINE)
_BOLD_ITALIC_RE = re.compile(r"[*_]{1,3}")
_TOKENIZE_RE = re.compile(r"[^\w-]+")


class SearchEngine:
    """BM25 search with adaptive wiki-link graph traversal."""

    def __init__(
        self,
        memory_storage: MemoryStorage,
        index_manager: IndexManager,
        config: TeamMemoryConfig,
    ) -> None:
        self._storage = memory_storage
        self._index_mgr = index_manager
        self._config = config
        # Corpus cache: invalidated when index entity set or content changes
        self._corpus_cache: list[list[str]] | None = None
        self._entity_ids_cache: list[str] | None = None
        self._contents_cache: dict[str, str] | None = None
        self._cached_index_fingerprint: frozenset[tuple[str, str]] | None = None

    async def search(
        self,
        query: str,
        max_results: int = 20,
        agent_classification: Classification = Classification.UNCLASSIFIED,
    ) -> list[SearchResult]:
        """Full search: BM25 initial -> adaptive traversal -> ranked results."""
        if not query.strip():
            return []

        index = await self._index_mgr.get_index()
        if not index:
            return []

        # Build or refresh corpus
        corpus, entity_ids, _contents = await self._get_corpus(index)
        if not corpus:
            return []

        # Phase 1: BM25 scoring
        initial = self._bm25_search(query, corpus, entity_ids, max_results)
        if not initial:
            return []

        # Phase 2: Wiki-link traversal (classification-aware)
        # Build per-entity token cache for traversal scoring
        corpus_cache = dict(zip(entity_ids, corpus, strict=True))
        traversal = await self._traverse_links(
            initial, query, corpus_cache, index, agent_classification
        )

        # Merge: initial results at hop 0, traversal adds more
        seen: dict[str, tuple[float, int]] = {}
        for eid, score in initial:
            seen[eid] = (score, 0)
        for eid, score, hops in traversal:
            if eid not in seen or score > seen[eid][0]:
                seen[eid] = (score, hops)

        # Sort by score descending, limit results
        ranked = sorted(seen.items(), key=lambda x: x[1][0], reverse=True)[:max_results]

        return [
            SearchResult(
                entity_id=eid,
                path=index[eid].path if eid in index else "",
                score=score,
                hops=hops,
                snippet=index[eid].summary_snippet if eid in index else "",
                entity_type=index[eid].entity_type if eid in index else "",
                tags=index[eid].tags if eid in index else [],
                classification=index[eid].classification if eid in index else "unclassified",
            )
            for eid, (score, hops) in ranked
        ]

    async def _get_corpus(
        self,
        index: dict[str, IndexEntry],
    ) -> tuple[list[list[str]], list[str], dict[str, str]]:
        """Get or rebuild tokenized corpus.

        Invalidates when index keys OR last_updated timestamps change,
        ensuring content updates are reflected.
        """
        fingerprint = frozenset((eid, entry.last_updated) for eid, entry in index.items())
        if self._corpus_cache is not None and self._cached_index_fingerprint == fingerprint:
            return self._corpus_cache, self._entity_ids_cache or [], self._contents_cache or {}

        # Read all entity files
        contents: dict[str, str] = {}
        for eid, entry in index.items():
            path = self._storage.resolve_entity_path(entry.path)
            raw = await asyncio.to_thread(self._read_file, path)
            if raw:
                contents[eid] = raw

        entity_ids = list(contents.keys())
        corpus = [self._tokenize(self._strip_markdown(contents[eid])) for eid in entity_ids]

        self._corpus_cache = corpus
        self._entity_ids_cache = entity_ids
        self._contents_cache = contents
        self._cached_index_fingerprint = fingerprint
        return corpus, entity_ids, contents

    def _bm25_search(
        self,
        query: str,
        corpus: list[list[str]],
        entity_ids: list[str],
        top_k: int,
    ) -> list[tuple[str, float]]:
        """BM25Plus scoring. Returns (entity_id, score) pairs.

        Requires at least one query token to appear in the document
        (BM25Plus assigns positive scores even to non-matching docs).
        """
        if not corpus:
            return []

        bm25 = BM25Plus(corpus)
        query_tokens = self._tokenize(query)
        query_token_set = set(query_tokens)
        scores = bm25.get_scores(query_tokens)

        # Filter: require at least one query token in document
        scored = [
            (entity_ids[i], float(scores[i]))
            for i in range(len(entity_ids))
            if scores[i] > 0.0 and query_token_set & set(corpus[i])
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    async def _traverse_links(
        self,
        initial_results: list[tuple[str, float]],
        query: str,
        corpus_cache: dict[str, list[str]],
        index: dict[str, IndexEntry],
        agent_classification: Classification = Classification.UNCLASSIFIED,
    ) -> list[tuple[str, float, int]]:
        """BFS wiki-link traversal with adaptive BM25 stopping.

        Classification-aware: prunes neighbors above agent clearance
        to prevent information leakage via score influence.

        Returns (entity_id, score, hops) triples.
        """
        if not initial_results:
            return []

        max_score = max(score for _, score in initial_results)
        threshold = self._config.bm25_threshold_ratio * max_score
        max_hops = self._config.max_hops

        visited: set[str] = {eid for eid, _ in initial_results}
        results: list[tuple[str, float, int]] = []

        # BFS queue using deque for O(1) popleft
        bfs_queue: deque[tuple[str, int]] = deque((eid, 0) for eid, _ in initial_results)

        while bfs_queue:
            current_id, current_hop = bfs_queue.popleft()
            if current_hop >= max_hops:
                continue

            entry = index.get(current_id)
            if entry is None:
                continue

            # Follow outgoing links + backlinks
            neighbors = set(entry.links_to) | set(entry.linked_from)

            for neighbor_id in neighbors:
                if neighbor_id in visited:
                    continue
                neighbor_entry = index.get(neighbor_id)
                if neighbor_entry is None:
                    continue
                visited.add(neighbor_id)

                # Classification pruning: skip entities above agent clearance
                neighbor_level = ClassificationChecker.parse_classification(
                    neighbor_entry.classification
                )
                if agent_classification < neighbor_level:
                    continue

                # Score neighbor using BM25-style token overlap
                neighbor_tokens = corpus_cache.get(neighbor_id, [])
                score = self._score_tokens(query, neighbor_tokens)

                if score >= threshold:
                    results.append((neighbor_id, score, current_hop + 1))
                    bfs_queue.append((neighbor_id, current_hop + 1))

        return results

    def _score_tokens(self, query: str, tokens: list[str]) -> float:
        """Simple token overlap scoring for traversal (lightweight BM25 proxy)."""
        if not tokens:
            return 0.0
        query_tokens = set(self._tokenize(query))
        if not query_tokens:
            return 0.0
        overlap = query_tokens & set(tokens)
        return len(overlap) / len(query_tokens)

    def _tokenize(self, text: str) -> list[str]:
        """Lowercase, split on whitespace + punctuation, keep hyphens."""
        tokens = _TOKENIZE_RE.split(text.lower())
        return [t for t in tokens if t]

    def _strip_markdown(self, text: str) -> str:
        """Remove YAML frontmatter, markdown syntax, code blocks, keep wiki-link text."""
        # Remove frontmatter
        result = _FRONTMATTER_RE.sub("", text)
        # Remove code blocks
        result = _CODE_BLOCK_RE.sub("", result)
        # Replace wiki-links with their text
        result = _WIKI_LINK_RE.sub(r"\1", result)
        # Remove heading markers
        result = _HEADING_RE.sub("", result)
        # Remove bold/italic markers
        result = _BOLD_ITALIC_RE.sub("", result)
        return result

    @staticmethod
    def _read_file(path: Path) -> str | None:
        """Read file contents as string."""
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
