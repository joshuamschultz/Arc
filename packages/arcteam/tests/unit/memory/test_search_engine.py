"""Tests for SearchEngine (BM25 + wiki-link traversal)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.index_manager import IndexManager
from arcteam.memory.search_engine import SearchEngine
from arcteam.memory.storage import MemoryStorage
from arcteam.memory.types import EntityMetadata


def _make_metadata(**overrides: object) -> EntityMetadata:
    defaults = {
        "entity_type": "person",
        "entity_id": "john-doe",
        "name": "John Doe",
        "last_updated": "2026-02-21",
    }
    defaults.update(overrides)
    return EntityMetadata(**defaults)  # type: ignore[arg-type]


@pytest.fixture
async def setup(tmp_path: Path) -> tuple[SearchEngine, MemoryStorage, IndexManager, TeamMemoryConfig]:
    config = TeamMemoryConfig(root=tmp_path)
    storage = MemoryStorage(config.entities_dir)
    index_mgr = IndexManager(config.entities_dir, storage, config)
    engine = SearchEngine(storage, index_mgr, config)
    return engine, storage, index_mgr, config


class TestSearchBasic:
    """Basic BM25 search functionality."""

    @pytest.mark.asyncio
    async def test_search_empty_index(self, setup: tuple) -> None:
        engine, _, _, _ = setup
        results = await engine.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_empty_query(self, setup: tuple) -> None:
        engine, storage, index_mgr, _ = setup
        await storage.write_entity("alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice\n\nA researcher.")
        await index_mgr.rebuild()
        results = await engine.search("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_finds_relevant_entity(self, setup: tuple) -> None:
        engine, storage, index_mgr, _ = setup
        await storage.write_entity(
            "alice",
            _make_metadata(entity_id="alice", name="Alice"),
            "# Alice\n\nAlice is a nuclear physicist at Los Alamos.",
        )
        await storage.write_entity(
            "bob",
            _make_metadata(entity_id="bob", name="Bob"),
            "# Bob\n\nBob is a software engineer in Denver.",
        )
        await index_mgr.rebuild()
        results = await engine.search("nuclear physicist")
        assert len(results) > 0
        assert results[0].entity_id == "alice"

    @pytest.mark.asyncio
    async def test_search_returns_search_result_type(self, setup: tuple) -> None:
        engine, storage, index_mgr, _ = setup
        await storage.write_entity(
            "alice",
            _make_metadata(entity_id="alice", name="Alice"),
            "# Alice\n\nA researcher in quantum computing.",
        )
        await index_mgr.rebuild()
        results = await engine.search("quantum computing")
        assert len(results) > 0
        result = results[0]
        assert result.entity_id == "alice"
        assert result.score > 0.0
        assert isinstance(result.hops, int)

    @pytest.mark.asyncio
    async def test_search_respects_max_results(self, setup: tuple) -> None:
        engine, storage, index_mgr, _ = setup
        # Create 5 entities about physics
        for i in range(5):
            eid = f"person-{i}"
            await storage.write_entity(
                eid,
                _make_metadata(entity_id=eid, name=f"Person {i}"),
                f"# Person {i}\n\nThis person studies physics and science.",
            )
        await index_mgr.rebuild()
        results = await engine.search("physics science", max_results=2)
        assert len(results) <= 2


class TestWikiLinkTraversal:
    """Wiki-link traversal should discover connected entities."""

    @pytest.mark.asyncio
    async def test_traversal_discovers_linked_entity(self, setup: tuple) -> None:
        engine, storage, index_mgr, _ = setup
        # Alice mentions nuclear, links to lab
        await storage.write_entity(
            "alice",
            _make_metadata(entity_id="alice", name="Alice", links_to=["los-alamos"]),
            "# Alice\n\nAlice is a nuclear physicist.",
        )
        # Los Alamos mentions nuclear, linked FROM alice
        await storage.write_entity(
            "los-alamos",
            _make_metadata(entity_id="los-alamos", name="Los Alamos", entity_type="organization"),
            "# Los Alamos\n\nNational nuclear laboratory in New Mexico.",
        )
        await index_mgr.rebuild()
        results = await engine.search("nuclear physicist")
        entity_ids = [r.entity_id for r in results]
        assert "alice" in entity_ids
        # Los Alamos should also be found (either via BM25 or traversal)
        assert "los-alamos" in entity_ids

    @pytest.mark.asyncio
    async def test_traversal_respects_max_hops(self, setup: tuple) -> None:
        engine, storage, index_mgr, config = setup
        # Chain: A -> B -> C -> D with max_hops=1
        config_limited = TeamMemoryConfig(root=config.root, max_hops=1)
        index_mgr_limited = IndexManager(config_limited.entities_dir, storage, config_limited)
        engine_limited = SearchEngine(storage, index_mgr_limited, config_limited)

        await storage.write_entity(
            "a", _make_metadata(entity_id="a", name="A", links_to=["b"]),
            "# A\n\nUnique term xylophone research.",
        )
        await storage.write_entity(
            "b", _make_metadata(entity_id="b", name="B", links_to=["c"]),
            "# B\n\nB collaborates with A.",
        )
        await storage.write_entity(
            "c", _make_metadata(entity_id="c", name="C", links_to=["d"]),
            "# C\n\nC is distant from A.",
        )
        await storage.write_entity(
            "d", _make_metadata(entity_id="d", name="D"),
            "# D\n\nD is very distant from A.",
        )
        await index_mgr_limited.rebuild()
        results = await engine_limited.search("xylophone research")
        entity_ids = [r.entity_id for r in results]
        # A should be found via BM25
        assert "a" in entity_ids
        # With max_hops=1, should not traverse beyond B
        # D should not be in results
        assert "d" not in entity_ids


class TestTokenization:
    """_tokenize and _strip_markdown should handle edge cases."""

    def test_tokenize_basic(self, setup: tuple) -> None:
        engine, _, _, _ = setup
        tokens = engine._tokenize("Hello World test")
        assert tokens == ["hello", "world", "test"]

    def test_tokenize_punctuation(self, setup: tuple) -> None:
        engine, _, _, _ = setup
        tokens = engine._tokenize("hello, world! test-case")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test-case" in tokens

    def test_strip_markdown_headings(self, setup: tuple) -> None:
        engine, _, _, _ = setup
        result = engine._strip_markdown("# Heading\n\n## Sub\n\nContent")
        assert "#" not in result
        assert "Content" in result

    def test_strip_markdown_wiki_links(self, setup: tuple) -> None:
        engine, _, _, _ = setup
        result = engine._strip_markdown("Links to [[alice]] and [[bob]]")
        assert "alice" in result
        assert "bob" in result
        assert "[[" not in result

    def test_strip_markdown_code_blocks(self, setup: tuple) -> None:
        engine, _, _, _ = setup
        text = "Before\n```python\ncode = True\n```\nAfter"
        result = engine._strip_markdown(text)
        assert "Before" in result
        assert "After" in result
        assert "code = True" not in result

    def test_strip_markdown_frontmatter(self, setup: tuple) -> None:
        engine, _, _, _ = setup
        text = "---\nentity_id: alice\n---\nContent here"
        result = engine._strip_markdown(text)
        assert "entity_id" not in result
        assert "Content here" in result


class TestCorpusCaching:
    """Corpus should be cached and invalidated on dirty flag."""

    @pytest.mark.asyncio
    async def test_corpus_invalidated_on_dirty(self, setup: tuple) -> None:
        engine, storage, index_mgr, _ = setup
        await storage.write_entity(
            "alice",
            _make_metadata(entity_id="alice", name="Alice"),
            "# Alice\n\nOriginal content about biology.",
        )
        await index_mgr.rebuild()
        # First search populates corpus cache
        results1 = await engine.search("biology")
        assert len(results1) > 0

        # Add new entity and touch dirty
        await storage.write_entity(
            "bob",
            _make_metadata(entity_id="bob", name="Bob"),
            "# Bob\n\nBob studies biology at university.",
        )
        await index_mgr.touch_dirty()
        # Second search should pick up bob
        results2 = await engine.search("biology")
        entity_ids = [r.entity_id for r in results2]
        assert "bob" in entity_ids
