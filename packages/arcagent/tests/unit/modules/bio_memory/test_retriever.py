"""Tests for Retriever — grep-based search, wiki-link following, budget enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.retriever import RetrievalResult, Retriever


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def config() -> BioMemoryConfig:
    return BioMemoryConfig()


@pytest.fixture
def retriever(memory_dir: Path, config: BioMemoryConfig) -> Retriever:
    return Retriever(memory_dir=memory_dir, config=config)


def _write_episode(
    memory_dir: Path,
    name: str,
    frontmatter: dict[str, object],
    body: str,
) -> Path:
    """Helper to create an episode markdown file."""
    episodes = memory_dir / "episodes"
    episodes.mkdir(exist_ok=True)
    fm_text = yaml.dump(frontmatter, default_flow_style=False).strip()
    content = f"---\n{fm_text}\n---\n\n{body}\n"
    path = episodes / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestSearch:
    """Retriever.search() returns scored results."""

    @pytest.mark.asyncio
    async def test_search_empty_dir_returns_empty(
        self, retriever: Retriever,
    ) -> None:
        results = await retriever.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_finds_matching_episode(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        _write_episode(
            memory_dir,
            "2026-02-21-deadline-change",
            {"tags": ["deadline", "project"], "entities": ["ProjectX"]},
            "The deadline for ProjectX was moved to March.",
        )
        results = await retriever.search("deadline")
        assert len(results) >= 1
        assert any("deadline" in r.source.lower() or "deadline" in r.content.lower()
                    for r in results)

    @pytest.mark.asyncio
    async def test_search_returns_retrieval_result_type(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        _write_episode(
            memory_dir,
            "2026-02-20-test",
            {"tags": ["test"]},
            "Some test content.",
        )
        results = await retriever.search("test")
        assert len(results) >= 1
        assert isinstance(results[0], RetrievalResult)
        assert results[0].source
        assert results[0].content
        assert isinstance(results[0].score, float)
        assert results[0].match_type in ("frontmatter", "fulltext", "wiki_link")

    @pytest.mark.asyncio
    async def test_search_respects_top_k(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        for i in range(5):
            _write_episode(
                memory_dir,
                f"2026-02-{20+i}-event{i}",
                {"tags": ["common"]},
                f"Event {i} with common keyword.",
            )
        results = await retriever.search("common", top_k=2)
        assert len(results) <= 2

    @pytest.mark.asyncio
    async def test_search_includes_working_md(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        (memory_dir / "working.md").write_text(
            "---\ntags: [active]\n---\n\nCurrent active task.",
            encoding="utf-8",
        )
        results = await retriever.search("active")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_includes_identity(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        (memory_dir / "how-i-work.md").write_text(
            "I prefer structured responses.", encoding="utf-8",
        )
        results = await retriever.search("structured")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_with_scope_filters_results(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        """Scope parameter filters which files are searched."""
        _write_episode(
            memory_dir, "2026-02-21-ep", {"tags": ["target"]}, "target keyword",
        )
        (memory_dir / "working.md").write_text(
            "---\ntags: [target]\n---\n\ntarget keyword in working",
            encoding="utf-8",
        )
        # Search only episodes — should not find working.md
        results = await retriever.search("target", scope="episodes")
        sources = [r.source for r in results]
        assert not any("working.md" in s for s in sources)


class TestRecall:
    """Retriever.recall() retrieves specific entity/episode by name."""

    @pytest.mark.asyncio
    async def test_recall_existing_episode(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        _write_episode(
            memory_dir,
            "2026-02-21-deadline-change",
            {"tags": ["deadline"]},
            "Deadline moved.",
        )
        result = await retriever.recall("2026-02-21-deadline-change")
        assert result is not None
        assert "Deadline moved" in result

    @pytest.mark.asyncio
    async def test_recall_missing_returns_none(
        self, retriever: Retriever,
    ) -> None:
        result = await retriever.recall("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_recall_path_traversal_blocked(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        """Path traversal via ../.. in name must return None (SEC-9)."""
        # Create a file outside memory_dir
        secret = memory_dir.parent / "secret.md"
        secret.write_text("confidential data", encoding="utf-8")
        result = await retriever.recall("../secret")
        assert result is None

    @pytest.mark.asyncio
    async def test_recall_absolute_path_traversal_blocked(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        """Absolute path in name must not escape memory_dir."""
        result = await retriever.recall("/etc/passwd")
        assert result is None


class TestWikiLinkFollowing:
    """Wiki-link extraction and one-hop following."""

    @pytest.mark.asyncio
    async def test_wiki_link_followed(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        """Search result from a file with [[wiki-link]] pulls in linked file."""
        _write_episode(
            memory_dir,
            "2026-02-21-meeting",
            {"tags": ["meeting"]},
            "Discussed [[project-alpha]] timeline.",
        )
        _write_episode(
            memory_dir,
            "project-alpha",
            {"tags": ["project"]},
            "Project Alpha is a key initiative.",
        )
        results = await retriever.search("meeting")
        # Should have both the meeting episode and the linked project-alpha
        sources = [r.source for r in results]
        assert any("project-alpha" in s for s in sources)

    @pytest.mark.asyncio
    async def test_dangling_wiki_link_ignored(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        """Wiki-links to nonexistent files are silently skipped."""
        _write_episode(
            memory_dir,
            "2026-02-21-note",
            {"tags": ["note"]},
            "Mentioned [[nonexistent-entity]] here.",
        )
        results = await retriever.search("note")
        # Should still return the note but not crash
        assert len(results) >= 1
        assert not any("nonexistent-entity" in r.source for r in results)


class TestDiscoverFiles:
    """File discovery finds all indexable files."""

    def test_discovers_episodes(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1 body")
        _write_episode(memory_dir, "ep2", {"tags": []}, "Ep2 body")
        files = retriever._discover_files()
        assert len(files) == 2

    def test_discovers_working_md(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        (memory_dir / "working.md").write_text("data", encoding="utf-8")
        files = retriever._discover_files()
        assert any(f.name == "working.md" for f in files)

    def test_discovers_identity(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        (memory_dir / "how-i-work.md").write_text("identity", encoding="utf-8")
        files = retriever._discover_files()
        assert any(f.name == "how-i-work.md" for f in files)

    def test_empty_dir_returns_empty(self, retriever: Retriever) -> None:
        files = retriever._discover_files()
        assert files == []

    def test_scope_episodes_only(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        """Scope='episodes' excludes identity and working files."""
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1")
        (memory_dir / "working.md").write_text("data", encoding="utf-8")
        (memory_dir / "how-i-work.md").write_text("id", encoding="utf-8")
        files = retriever._discover_files(scope="episodes")
        assert all("episodes" in str(f) for f in files)
        assert len(files) == 1

    def test_scope_identity_only(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        """Scope='identity' returns only how-i-work.md."""
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1")
        (memory_dir / "how-i-work.md").write_text("identity", encoding="utf-8")
        files = retriever._discover_files(scope="identity")
        assert len(files) == 1
        assert files[0].name == "how-i-work.md"

    def test_scope_working_only(
        self, retriever: Retriever, memory_dir: Path,
    ) -> None:
        """Scope='working' returns only working.md."""
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1")
        (memory_dir / "working.md").write_text("work", encoding="utf-8")
        files = retriever._discover_files(scope="working")
        assert len(files) == 1
        assert files[0].name == "working.md"


class TestBudgetEnforcement:
    """Results are trimmed to fit within retrieved_budget."""

    @pytest.mark.asyncio
    async def test_enforce_budget_trims_results(
        self, memory_dir: Path,
    ) -> None:
        cfg = BioMemoryConfig(retrieved_budget=5)  # ~20 chars budget
        ret = Retriever(memory_dir=memory_dir, config=cfg)
        # Create episodes with substantial content
        for i in range(5):
            _write_episode(
                memory_dir,
                f"2026-02-{20+i}-big{i}",
                {"tags": ["big"]},
                "x" * 200,
            )
        results = await ret.search("big")
        # Total content should be within budget
        total_chars = sum(len(r.content) for r in results)
        max_chars = 5 * 4  # retrieved_budget * CHARS_PER_TOKEN
        # Either fewer results or truncated content
        assert total_chars <= max_chars + 200 or len(results) < 5


class TestRetrievalResult:
    """RetrievalResult dataclass has correct fields."""

    def test_fields(self) -> None:
        r = RetrievalResult(
            source="episodes/test.md",
            content="Test content",
            score=0.95,
            match_type="fulltext",
        )
        assert r.source == "episodes/test.md"
        assert r.content == "Test content"
        assert r.score == 0.95
        assert r.match_type == "fulltext"
