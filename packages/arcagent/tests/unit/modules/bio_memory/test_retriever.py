"""Tests for Retriever — search, wiki-links, budget enforcement, entity scope."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.retriever import RetrievalResult, Retriever


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def memory_dir(workspace: Path) -> Path:
    d = workspace / "memory"
    d.mkdir()
    return d


@pytest.fixture
def entities_dir(workspace: Path) -> Path:
    d = workspace / "entities"
    d.mkdir()
    return d


@pytest.fixture
def config() -> BioMemoryConfig:
    return BioMemoryConfig()


@pytest.fixture
def retriever(memory_dir: Path, config: BioMemoryConfig, workspace: Path) -> Retriever:
    return Retriever(memory_dir=memory_dir, config=config, workspace=workspace)


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


def _write_entity(
    entities_dir: Path,
    name: str,
    frontmatter: dict[str, object],
    body: str,
    subdir: str | None = None,
) -> Path:
    """Helper to create an entity markdown file."""
    target_dir = entities_dir / subdir if subdir else entities_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.dump(frontmatter, default_flow_style=False).strip()
    content = f"---\n{fm_text}\n---\n\n{body}\n"
    path = target_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestSearch:
    """Retriever.search() returns scored results."""

    @pytest.mark.asyncio
    async def test_search_empty_dir_returns_empty(
        self,
        retriever: Retriever,
    ) -> None:
        results = await retriever.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_finds_matching_episode(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        _write_episode(
            memory_dir,
            "2026-02-21-deadline-change",
            {"tags": ["deadline", "project"], "entities": ["ProjectX"]},
            "The deadline for ProjectX was moved to March.",
        )
        results = await retriever.search("deadline")
        assert len(results) >= 1
        assert any(
            "deadline" in r.source.lower() or "deadline" in r.content.lower() for r in results
        )

    @pytest.mark.asyncio
    async def test_search_returns_retrieval_result_type(
        self,
        retriever: Retriever,
        memory_dir: Path,
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
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        for i in range(5):
            _write_episode(
                memory_dir,
                f"2026-02-{20 + i}-event{i}",
                {"tags": ["common"]},
                f"Event {i} with common keyword.",
            )
        results = await retriever.search("common", top_k=2)
        assert len(results) <= 2

    @pytest.mark.asyncio
    async def test_search_includes_working_md(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        (memory_dir / "working.md").write_text(
            "---\ntags: [active]\n---\n\nCurrent active task.",
            encoding="utf-8",
        )
        results = await retriever.search("active")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_includes_daily_notes(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        dn_dir = memory_dir / "daily-notes"
        dn_dir.mkdir(parents=True, exist_ok=True)
        (dn_dir / "2026-02-25.md").write_text(
            "---\ndate: '2026-02-25'\n---\n\n# 2026-02-25\n\n- Prefer structured responses.\n",
            encoding="utf-8",
        )
        results = await retriever.search("structured")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_with_scope_filters_results(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        """Scope parameter filters which files are searched."""
        _write_episode(
            memory_dir,
            "2026-02-21-ep",
            {"tags": ["target"]},
            "target keyword",
        )
        (memory_dir / "working.md").write_text(
            "---\ntags: [target]\n---\n\ntarget keyword in working",
            encoding="utf-8",
        )
        # Search only episodes — should not find working.md
        results = await retriever.search("target", scope="episodes")
        sources = [r.source for r in results]
        assert not any("working.md" in s for s in sources)


class TestEntitySearch:
    """Retriever searches workspace/entities/ when scope is entities."""

    @pytest.mark.asyncio
    async def test_search_finds_entities(
        self,
        retriever: Retriever,
        entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir,
            "josh-schultz",
            {"entity_type": "person", "tags": ["founder"]},
            "Josh is the founder of CTG Federal.",
        )
        results = await retriever.search("founder", scope="entities")
        assert len(results) >= 1
        assert any("josh-schultz" in r.source for r in results)

    @pytest.mark.asyncio
    async def test_search_finds_entities_in_subdirectories(
        self,
        retriever: Retriever,
        entities_dir: Path,
    ) -> None:
        _write_entity(
            entities_dir,
            "ctg-federal",
            {"entity_type": "org", "tags": ["company"]},
            "CTG Federal is a government contractor.",
            subdir="orgs",
        )
        results = await retriever.search("contractor", scope="entities")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_none_scope_includes_entities(
        self,
        retriever: Retriever,
        memory_dir: Path,
        entities_dir: Path,
    ) -> None:
        """Scope=None searches all tiers including entities."""
        _write_episode(memory_dir, "ep1", {"tags": ["alpha"]}, "alpha content")
        _write_entity(entities_dir, "alpha-project", {"tags": ["alpha"]}, "alpha entity")
        results = await retriever.search("alpha")
        sources = " ".join(r.source for r in results)
        assert "alpha-project" in sources

    @pytest.mark.asyncio
    async def test_search_entities_scope_excludes_episodes(
        self,
        retriever: Retriever,
        memory_dir: Path,
        entities_dir: Path,
    ) -> None:
        _write_episode(memory_dir, "ep1", {"tags": ["beta"]}, "beta ep")
        _write_entity(entities_dir, "beta-ent", {"tags": ["beta"]}, "beta entity")
        results = await retriever.search("beta", scope="entities")
        sources = [r.source for r in results]
        assert not any("episodes" in s for s in sources)


class TestTeamEntitySearch:
    """Retriever includes team entities as first-class results."""

    @pytest.mark.asyncio
    async def test_team_entities_included_in_search(
        self,
        memory_dir: Path,
        workspace: Path,
    ) -> None:
        # Create team entities dir
        team_dir = workspace / "team_entities"
        team_dir.mkdir()
        _write_entity(team_dir, "shared-entity", {"tags": ["target"]}, "target keyword " * 5)

        # Create local entity with same keyword
        entities_dir = workspace / "entities"
        entities_dir.mkdir()
        _write_entity(entities_dir, "local-entity", {"tags": ["target"]}, "target keyword " * 5)

        cfg = BioMemoryConfig()
        ret = Retriever(memory_dir, cfg, workspace=workspace, team_entities_dir=team_dir)
        results = await ret.search("target", scope="entities")

        local = [r for r in results if "local-entity" in r.source]
        team = [r for r in results if "shared-entity" in r.source]
        assert len(local) >= 1
        assert len(team) >= 1
        # Team entities score equally — no penalty
        assert team[0].score == local[0].score

    @pytest.mark.asyncio
    async def test_team_entities_prefixed_in_source(
        self,
        memory_dir: Path,
        workspace: Path,
    ) -> None:
        """Team entity results show team/ prefix in source path."""
        team_dir = workspace / "team_entities"
        team_dir.mkdir()
        _write_entity(team_dir, "shared-thing", {"tags": ["unique"]}, "unique keyword")

        cfg = BioMemoryConfig()
        ret = Retriever(memory_dir, cfg, workspace=workspace, team_entities_dir=team_dir)
        results = await ret.search("unique", scope="team")

        assert len(results) >= 1
        assert results[0].source.startswith("team/")

    @pytest.mark.asyncio
    async def test_team_scope_excludes_local(
        self,
        memory_dir: Path,
        workspace: Path,
    ) -> None:
        """scope='team' returns only team entities, not local."""
        team_dir = workspace / "team_entities"
        team_dir.mkdir()
        _write_entity(team_dir, "shared-only", {"tags": ["keyword"]}, "keyword content")

        entities_dir = workspace / "entities"
        entities_dir.mkdir()
        _write_entity(entities_dir, "local-only", {"tags": ["keyword"]}, "keyword content")

        cfg = BioMemoryConfig()
        ret = Retriever(memory_dir, cfg, workspace=workspace, team_entities_dir=team_dir)
        results = await ret.search("keyword", scope="team")

        sources = [r.source for r in results]
        assert any("shared-only" in s for s in sources)
        assert not any("local-only" in s for s in sources)


class TestRecall:
    """Retriever.recall() retrieves specific entity/episode by name."""

    @pytest.mark.asyncio
    async def test_recall_existing_episode(
        self,
        retriever: Retriever,
        memory_dir: Path,
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
        self,
        retriever: Retriever,
    ) -> None:
        result = await retriever.recall("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_recall_path_traversal_blocked(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        """Path traversal via ../.. in name must return None (SEC-9)."""
        # Create a file outside memory_dir
        secret = memory_dir.parent / "secret.md"
        secret.write_text("confidential data", encoding="utf-8")
        result = await retriever.recall("../secret")
        assert result is None

    @pytest.mark.asyncio
    async def test_recall_absolute_path_traversal_blocked(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        """Absolute path in name must not escape memory_dir."""
        result = await retriever.recall("/etc/passwd")
        assert result is None

    @pytest.mark.asyncio
    async def test_recall_entity_by_name(
        self,
        retriever: Retriever,
        entities_dir: Path,
    ) -> None:
        """recall() finds entity files in workspace/entities/."""
        _write_entity(entities_dir, "josh-schultz", {}, "Josh's entity file.")
        result = await retriever.recall("josh-schultz")
        assert result is not None
        assert "Josh's entity file" in result

    @pytest.mark.asyncio
    async def test_recall_entity_in_subdirectory(
        self,
        retriever: Retriever,
        entities_dir: Path,
    ) -> None:
        """recall() finds entities in subdirectories."""
        _write_entity(
            entities_dir,
            "ctg-federal",
            {},
            "CTG entity.",
            subdir="orgs",
        )
        result = await retriever.recall("ctg-federal")
        assert result is not None
        assert "CTG entity" in result


class TestWikiLinkFollowing:
    """Wiki-link extraction and one-hop following."""

    @pytest.mark.asyncio
    async def test_wiki_link_followed(
        self,
        retriever: Retriever,
        memory_dir: Path,
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
        self,
        retriever: Retriever,
        memory_dir: Path,
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

    @pytest.mark.asyncio
    async def test_wiki_link_resolves_to_entity(
        self,
        retriever: Retriever,
        memory_dir: Path,
        entities_dir: Path,
    ) -> None:
        """Wiki-link [[entity-id]] resolves to workspace/entities/ files."""
        _write_episode(
            memory_dir,
            "2026-02-21-note",
            {"tags": ["note"]},
            "Discussed [[josh-schultz]] today.",
        )
        _write_entity(entities_dir, "josh-schultz", {"tags": ["person"]}, "Josh entity.")
        results = await retriever.search("note")
        sources = [r.source for r in results]
        assert any("josh-schultz" in s for s in sources)


class TestDiscoverFiles:
    """File discovery finds all indexable files."""

    def test_discovers_episodes(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1 body")
        _write_episode(memory_dir, "ep2", {"tags": []}, "Ep2 body")
        files = retriever._discover_files()
        assert len(files) >= 2

    def test_discovers_working_md(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        (memory_dir / "working.md").write_text("data", encoding="utf-8")
        files = retriever._discover_files()
        assert any(f.name == "working.md" for f in files)

    def test_discovers_daily_notes(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        dn_dir = memory_dir / "daily-notes"
        dn_dir.mkdir(parents=True, exist_ok=True)
        (dn_dir / "2026-02-25.md").write_text("daily note content", encoding="utf-8")
        files = retriever._discover_files()
        assert any(f.name == "2026-02-25.md" for f in files)

    def test_empty_dir_returns_empty(self, retriever: Retriever) -> None:
        files = retriever._discover_files()
        assert files == []

    def test_scope_episodes_only(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        """Scope='episodes' excludes daily notes and working files."""
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1")
        (memory_dir / "working.md").write_text("data", encoding="utf-8")
        dn_dir = memory_dir / "daily-notes"
        dn_dir.mkdir(parents=True, exist_ok=True)
        (dn_dir / "2026-02-25.md").write_text("note", encoding="utf-8")
        files = retriever._discover_files(scope="episodes")
        assert all("episodes" in str(f) for f in files)
        assert len(files) == 1

    def test_scope_daily_notes_only(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        """Scope='daily_notes' returns only daily note files."""
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1")
        dn_dir = memory_dir / "daily-notes"
        dn_dir.mkdir(parents=True, exist_ok=True)
        (dn_dir / "2026-02-25.md").write_text("daily note", encoding="utf-8")
        files = retriever._discover_files(scope="daily_notes")
        assert len(files) == 1
        assert files[0].name == "2026-02-25.md"

    def test_scope_working_only(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        """Scope='working' returns only working.md."""
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1")
        (memory_dir / "working.md").write_text("work", encoding="utf-8")
        files = retriever._discover_files(scope="working")
        assert len(files) == 1
        assert files[0].name == "working.md"

    def test_scope_entities_discovers_entity_files(
        self,
        retriever: Retriever,
        entities_dir: Path,
    ) -> None:
        """Scope='entities' discovers workspace/entities/ files."""
        _write_entity(entities_dir, "ent1", {}, "Entity one")
        _write_entity(entities_dir, "ent2", {}, "Entity two", subdir="orgs")
        files = retriever._discover_files(scope="entities")
        assert len(files) == 2

    def test_scope_entities_excludes_episodes(
        self,
        retriever: Retriever,
        memory_dir: Path,
        entities_dir: Path,
    ) -> None:
        _write_episode(memory_dir, "ep1", {"tags": []}, "Ep1")
        _write_entity(entities_dir, "ent1", {}, "Entity one")
        files = retriever._discover_files(scope="entities")
        assert all("entities" in str(f) for f in files)


class TestIsWithinBounds:
    """Path validation covers both memory and entities directories."""

    def test_memory_path_within_bounds(
        self,
        retriever: Retriever,
        memory_dir: Path,
    ) -> None:
        path = (memory_dir / "episodes" / "test.md").resolve()
        assert retriever._is_within_bounds(path)

    def test_entity_path_within_bounds(
        self,
        retriever: Retriever,
        entities_dir: Path,
    ) -> None:
        path = (entities_dir / "test.md").resolve()
        assert retriever._is_within_bounds(path)

    def test_outside_path_rejected(self, retriever: Retriever) -> None:
        path = Path("/etc/passwd").resolve()
        assert not retriever._is_within_bounds(path)


class TestBudgetEnforcement:
    """Results are trimmed to fit within retrieved_budget."""

    @pytest.mark.asyncio
    async def test_enforce_budget_trims_results(
        self,
        memory_dir: Path,
        workspace: Path,
    ) -> None:
        cfg = BioMemoryConfig(retrieved_budget=5)  # ~20 chars budget
        ret = Retriever(memory_dir=memory_dir, config=cfg, workspace=workspace)
        # Create episodes with substantial content
        for i in range(5):
            _write_episode(
                memory_dir,
                f"2026-02-{20 + i}-big{i}",
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
