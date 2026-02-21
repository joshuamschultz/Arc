"""Integration test: bio-memory retrieval through tools.

Tests search, recall, wiki-link following, budget enforcement,
and boundary markers using a seeded memory workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.retriever import Retriever


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def seeded_workspace(memory_dir: Path) -> Path:
    """Create a fully seeded memory workspace for retrieval testing."""
    # Identity
    (memory_dir / "how-i-work.md").write_text(
        "I prefer structured answers. I use wiki-links like [[project-alpha]].",
        encoding="utf-8",
    )

    # Working memory
    fm = yaml.dump(
        {"topics": ["testing", "retrieval"], "turn_number": 3},
        default_flow_style=False,
    ).strip()
    (memory_dir / "working.md").write_text(
        f"---\n{fm}\n---\n\nCurrently testing the retrieval system.",
        encoding="utf-8",
    )

    # Episodes
    episodes = memory_dir / "episodes"
    episodes.mkdir()

    for name, tags, entities, body in [
        (
            "2026-02-20-pricing-correction",
            ["pricing", "correction"],
            ["ClientA"],
            "Discovered pricing error. [[client-a]] was overcharged by 15%.",
        ),
        (
            "2026-02-21-deadline-change",
            ["deadline", "project"],
            ["ProjectX", "TeamLead"],
            "ProjectX deadline moved to March 15. [[project-x]] needs attention.",
        ),
        (
            "project-alpha",
            ["project"],
            [],
            "Project Alpha is the main initiative for Q1.",
        ),
        (
            "project-x",
            ["project"],
            [],
            "Project X involves migrating the legacy system.",
        ),
        (
            "client-a",
            ["client"],
            [],
            "Client A is our largest enterprise customer.",
        ),
    ]:
        fm_data = {"tags": tags, "entities": entities, "title": name}
        fm_text = yaml.dump(fm_data, default_flow_style=False).strip()
        (episodes / f"{name}.md").write_text(
            f"---\n{fm_text}\n---\n\n{body}\n",
            encoding="utf-8",
        )

    return memory_dir


class TestSearchIntegration:
    """End-to-end search across seeded workspace."""

    @pytest.mark.asyncio
    async def test_search_finds_episodes_by_tag(
        self, seeded_workspace: Path,
    ) -> None:
        retriever = Retriever(seeded_workspace, BioMemoryConfig())
        results = await retriever.search("pricing")
        assert len(results) >= 1
        assert any("pricing" in r.source for r in results)

    @pytest.mark.asyncio
    async def test_search_finds_by_fulltext(
        self, seeded_workspace: Path,
    ) -> None:
        retriever = Retriever(seeded_workspace, BioMemoryConfig())
        results = await retriever.search("deadline")
        assert len(results) >= 1
        assert any("deadline" in r.content.lower() for r in results)

    @pytest.mark.asyncio
    async def test_search_across_all_tiers(
        self, seeded_workspace: Path,
    ) -> None:
        """Search should find results in episodes, identity, and working memory."""
        retriever = Retriever(seeded_workspace, BioMemoryConfig())

        # Should find in identity
        results = await retriever.search("structured")
        assert any("how-i-work" in r.source for r in results)

        # Should find in working memory
        results = await retriever.search("retrieval")
        assert any("working" in r.source for r in results)

        # Should find in episodes
        results = await retriever.search("legacy")
        assert any("project-x" in r.source for r in results)


class TestWikiLinkIntegration:
    """Wiki-link following works end-to-end."""

    @pytest.mark.asyncio
    async def test_follows_wiki_links_from_episode(
        self, seeded_workspace: Path,
    ) -> None:
        retriever = Retriever(seeded_workspace, BioMemoryConfig())
        results = await retriever.search("pricing correction")

        # Should find the pricing episode AND the linked client-a
        sources = [r.source for r in results]
        assert any("pricing" in s for s in sources)
        assert any("client-a" in s for s in sources)

    @pytest.mark.asyncio
    async def test_follows_wiki_links_from_identity(
        self, seeded_workspace: Path,
    ) -> None:
        retriever = Retriever(seeded_workspace, BioMemoryConfig())
        results = await retriever.search("structured answers")

        # Identity mentions [[project-alpha]], should follow
        sources = [r.source for r in results]
        assert any("project-alpha" in s for s in sources)


class TestRecallIntegration:
    """Direct recall by name/slug."""

    @pytest.mark.asyncio
    async def test_recall_episode_by_name(
        self, seeded_workspace: Path,
    ) -> None:
        retriever = Retriever(seeded_workspace, BioMemoryConfig())
        result = await retriever.recall("project-alpha")
        assert result is not None
        assert "main initiative" in result

    @pytest.mark.asyncio
    async def test_recall_nonexistent(
        self, seeded_workspace: Path,
    ) -> None:
        retriever = Retriever(seeded_workspace, BioMemoryConfig())
        result = await retriever.recall("nonexistent-entity")
        assert result is None


class TestBudgetEnforcementIntegration:
    """Token budget is enforced across multi-file results."""

    @pytest.mark.asyncio
    async def test_results_within_budget(
        self, seeded_workspace: Path,
    ) -> None:
        # Very tight budget
        cfg = BioMemoryConfig(retrieved_budget=50)
        retriever = Retriever(seeded_workspace, cfg)
        results = await retriever.search("project")

        total_chars = sum(len(r.content) for r in results)
        max_chars = 50 * 4  # retrieved_budget * CHARS_PER_TOKEN
        assert total_chars <= max_chars


class TestRetrievalResultTypes:
    """Results have correct match_type values."""

    @pytest.mark.asyncio
    async def test_frontmatter_match_type(
        self, seeded_workspace: Path,
    ) -> None:
        retriever = Retriever(seeded_workspace, BioMemoryConfig())
        results = await retriever.search("pricing")
        # Tag match should be frontmatter type
        fm_results = [r for r in results if r.match_type == "frontmatter"]
        assert len(fm_results) >= 1

    @pytest.mark.asyncio
    async def test_wiki_link_match_type(
        self, seeded_workspace: Path,
    ) -> None:
        retriever = Retriever(seeded_workspace, BioMemoryConfig())
        results = await retriever.search("pricing correction")
        # Should have at least one wiki_link result (client-a)
        wl_results = [r for r in results if r.match_type == "wiki_link"]
        assert len(wl_results) >= 1
