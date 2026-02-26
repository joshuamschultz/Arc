"""Integration test: bio-memory isolation and team sharing.

Verifies that:
1. Agent A cannot see Agent B's private memory (episodes, working, daily notes)
2. Agent A cannot see Agent B's private entities
3. Both agents CAN see shared team entities
4. Team entity scoring is penalized vs private entities
5. Recall respects isolation boundaries
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.retriever import Retriever


def _write_entity(path: Path, name: str, body: str) -> None:
    """Write an entity file with v2.1 frontmatter."""
    fm = {
        "entity_type": "person",
        "entity_id": path.stem,
        "name": name,
        "status": "active",
        "tags": [],
        "links_to": [],
        "classification": "unclassified",
    }
    fm_text = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    path.write_text(f"---\n{fm_text}\n---\n\n# {name}\n\n{body}\n", encoding="utf-8")


def _write_episode(episodes_dir: Path, slug: str, tags: list[str], body: str) -> None:
    """Write an episode file."""
    fm = {"title": slug, "date": "2026-02-25", "tags": tags}
    fm_text = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    filename = f"2026-02-25-{slug}.md"
    (episodes_dir / filename).write_text(
        f"---\n{fm_text}\n---\n\n{body}\n", encoding="utf-8",
    )


@pytest.fixture
def team_layout(tmp_path: Path) -> dict[str, Path]:
    """Create a two-agent team layout with shared team entities.

    Layout:
        team/
            agent_a/
                workspace/
                    memory/ (episodes, working, daily-notes)
                    entities/ (private)
            agent_b/
                workspace/
                    memory/ (episodes, working, daily-notes)
                    entities/ (private)
            shared/
                entities/ (team-wide)
    """
    team = tmp_path / "team"

    # Agent A workspace
    ws_a = team / "agent_a" / "workspace"
    mem_a = ws_a / "memory"
    ep_a = mem_a / "episodes"
    dn_a = mem_a / "daily-notes"
    ent_a = ws_a / "entities"
    for d in [ep_a, dn_a, ent_a]:
        d.mkdir(parents=True)

    # Agent A daily notes
    dn_a_fm = yaml.dump({"date": "2026-02-25", "agent": "agent_a"}, default_flow_style=False).strip()
    (dn_a / "2026-02-25.md").write_text(
        f"---\n{dn_a_fm}\n---\n\n# 2026-02-25\n\n## 10:00 UTC\n- Agent A researched topic-x\n",
        encoding="utf-8",
    )
    # Agent A working memory
    fm = yaml.dump({"type": "note"}, default_flow_style=False).strip()
    (mem_a / "working.md").write_text(
        f"---\n{fm}\n---\n\nAgent A is researching topic-x.", encoding="utf-8",
    )
    # Agent A episode
    _write_episode(ep_a, "agent-a-discovered-bug", ["bug", "important"], "Agent A found a critical bug in module X.")
    # Agent A private entity
    _write_entity(ent_a / "alice-smith.md", "Alice Smith", "Alice is Agent A's primary contact.")

    # Agent B workspace
    ws_b = team / "agent_b" / "workspace"
    mem_b = ws_b / "memory"
    ep_b = mem_b / "episodes"
    dn_b = mem_b / "daily-notes"
    ent_b = ws_b / "entities"
    for d in [ep_b, dn_b, ent_b]:
        d.mkdir(parents=True)

    # Agent B daily notes
    dn_b_fm = yaml.dump({"date": "2026-02-25", "agent": "agent_b"}, default_flow_style=False).strip()
    (dn_b / "2026-02-25.md").write_text(
        f"---\n{dn_b_fm}\n---\n\n# 2026-02-25\n\n## 14:00 UTC\n- Agent B deployed service-y\n",
        encoding="utf-8",
    )
    # Agent B working memory
    fm = yaml.dump({"type": "note"}, default_flow_style=False).strip()
    (mem_b / "working.md").write_text(
        f"---\n{fm}\n---\n\nAgent B is deploying service-y.", encoding="utf-8",
    )
    # Agent B episode
    _write_episode(ep_b, "agent-b-deployed-service", ["deploy", "ops"], "Agent B deployed service Y to production.")
    # Agent B private entity
    _write_entity(ent_b / "bob-jones.md", "Bob Jones", "Bob is Agent B's primary contact.")

    # Shared team entities
    shared_ent = team / "shared" / "entities"
    shared_ent.mkdir(parents=True)
    _write_entity(shared_ent / "acme-corp.md", "Acme Corp", "Acme Corp is our primary customer. Both agents work with them.")
    _write_entity(shared_ent / "project-phoenix.md", "Project Phoenix", "Cross-team initiative led by both agents.")

    return {
        "team": team,
        "ws_a": ws_a,
        "ws_b": ws_b,
        "mem_a": mem_a,
        "mem_b": mem_b,
        "ent_a": ent_a,
        "ent_b": ent_b,
        "shared_ent": shared_ent,
    }


class TestPrivateMemoryIsolation:
    """Agent A cannot see Agent B's private memory."""

    def test_agent_a_cannot_see_agent_b_episodes(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        results = retriever_a._discover_files(scope="episodes")
        episode_names = [f.name for f in results]

        assert "2026-02-25-agent-a-discovered-bug.md" in episode_names
        assert "2026-02-25-agent-b-deployed-service.md" not in episode_names

    def test_agent_b_cannot_see_agent_a_episodes(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever_b = Retriever(
            team_layout["mem_b"], config,
            workspace=team_layout["ws_b"],
            team_entities_dir=team_layout["shared_ent"],
        )

        results = retriever_b._discover_files(scope="episodes")
        episode_names = [f.name for f in results]

        assert "2026-02-25-agent-b-deployed-service.md" in episode_names
        assert "2026-02-25-agent-a-discovered-bug.md" not in episode_names

    def test_agent_a_cannot_see_agent_b_daily_notes(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        files = retriever_a._discover_files(scope="daily_notes")
        contents = [f.read_text(encoding="utf-8") for f in files]
        combined = " ".join(contents)

        assert "Agent A" in combined
        assert "Agent B" not in combined

    def test_agent_a_cannot_see_agent_b_working_memory(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        files = retriever_a._discover_files(scope="working")
        contents = [f.read_text(encoding="utf-8") for f in files]
        combined = " ".join(contents)

        assert "topic-x" in combined
        assert "service-y" not in combined


class TestPrivateEntityIsolation:
    """Agent A cannot see Agent B's private entities."""

    def test_agent_a_sees_own_entities_not_agent_b(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        files = retriever_a._discover_files(scope="entities")
        names = [f.name for f in files]

        assert "alice-smith.md" in names
        assert "bob-jones.md" not in names

    def test_agent_b_sees_own_entities_not_agent_a(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever_b = Retriever(
            team_layout["mem_b"], config,
            workspace=team_layout["ws_b"],
            team_entities_dir=team_layout["shared_ent"],
        )

        files = retriever_b._discover_files(scope="entities")
        names = [f.name for f in files]

        assert "bob-jones.md" in names
        assert "alice-smith.md" not in names

    @pytest.mark.anyio
    async def test_recall_rejects_cross_agent_entity(
        self, team_layout: dict[str, Path],
    ) -> None:
        """memory_recall should not return another agent's entity."""
        config = BioMemoryConfig()
        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        # Can recall own entity
        result = await retriever_a.recall("alice-smith")
        assert result is not None
        assert "Alice" in result

        # Cannot recall agent B's entity
        result = await retriever_a.recall("bob-jones")
        assert result is None


class TestTeamEntitySharing:
    """Both agents can see shared team entities."""

    def test_agent_a_sees_team_entities(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        files = retriever_a._discover_files(scope="entities")
        names = [f.name for f in files]

        assert "acme-corp.md" in names
        assert "project-phoenix.md" in names

    def test_agent_b_sees_team_entities(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever_b = Retriever(
            team_layout["mem_b"], config,
            workspace=team_layout["ws_b"],
            team_entities_dir=team_layout["shared_ent"],
        )

        files = retriever_b._discover_files(scope="entities")
        names = [f.name for f in files]

        assert "acme-corp.md" in names
        assert "project-phoenix.md" in names

    @pytest.mark.anyio
    async def test_search_finds_team_entities(
        self, team_layout: dict[str, Path],
    ) -> None:
        """memory_search returns team entities for both agents."""
        config = BioMemoryConfig()
        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        results = await retriever_a.search("Acme Corp customer")
        sources = [r.source for r in results]

        assert any("acme-corp" in s for s in sources)

    @pytest.mark.anyio
    async def test_team_entities_scored_lower_than_private(
        self, team_layout: dict[str, Path],
    ) -> None:
        """Team entities receive a penalty vs private entities of equal relevance."""
        config = BioMemoryConfig()

        # Add a private entity about "customer" to agent A
        _write_entity(
            team_layout["ent_a"] / "internal-customer.md",
            "Internal Customer",
            "Internal customer contact for Acme Corp deals.",
        )

        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        results = await retriever_a.search("customer")
        # Find private vs team result
        private_results = [r for r in results if "internal-customer" in r.source]
        team_results = [r for r in results if "acme-corp" in r.source]

        assert len(private_results) > 0
        assert len(team_results) > 0
        # Private should score higher (no penalty)
        assert private_results[0].score > team_results[0].score


class TestNoTeamEntities:
    """When team_entities_dir is None, only private memory is searched."""

    def test_no_team_dir_only_private_entities(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=None,  # No team
        )

        files = retriever._discover_files(scope="entities")
        names = [f.name for f in files]

        assert "alice-smith.md" in names
        assert "acme-corp.md" not in names
        assert "project-phoenix.md" not in names

    @pytest.mark.anyio
    async def test_search_without_team_only_returns_private(
        self, team_layout: dict[str, Path],
    ) -> None:
        config = BioMemoryConfig()
        retriever = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=None,
        )

        results = await retriever.search("Acme Corp")
        sources = [r.source for r in results]

        assert not any("acme-corp" in s for s in sources)


class TestFullIsolationSearch:
    """End-to-end: search across all scopes respects isolation."""

    @pytest.mark.anyio
    async def test_unscoped_search_isolates_properly(
        self, team_layout: dict[str, Path],
    ) -> None:
        """An unscoped search (all tiers) should return:
        - Agent's own episodes, daily notes, working memory
        - Agent's own entities
        - Team entities
        - NOT the other agent's anything
        """
        config = BioMemoryConfig()
        retriever_a = Retriever(
            team_layout["mem_a"], config,
            workspace=team_layout["ws_a"],
            team_entities_dir=team_layout["shared_ent"],
        )

        # Search for "agent" which appears in daily notes, episodes, entities
        results = await retriever_a.search("agent")
        sources = [r.source for r in results]
        all_content = " ".join(r.content for r in results)

        # Should find Agent A's own content (daily notes or episodes)
        assert any("daily-notes" in s or "episodes" in s for s in sources)

        # Should NOT contain Agent B's content
        assert "Agent B" not in all_content
        assert "service-y" not in all_content
        assert "bob-jones" not in " ".join(sources)
