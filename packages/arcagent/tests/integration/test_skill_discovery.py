"""Integration test — skill discovery acceptance criteria (PRD §4.2).

Tests: create SKILL.md files → agent starts → all in prompt →
skills cached → agent-created rescan works.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def agent_config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="skill-test-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(
            did="",
            key_dir=str(tmp_path / "keys"),
            vault_path="",
        ),
        telemetry=TelemetryConfig(enabled=True),
    )


def _write_skill(skill_dir: Path, name: str, description: str) -> Path:
    """Write a skill .md file with YAML frontmatter."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / f"{name}.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: '1.0'\n"
        f"tags:\n  - test\n---\n\n# {name}\n\nFull skill content for {name}.\n"
    )
    return skill_file


class TestSkillDiscovery:
    """PRD §4.2 acceptance: skills discovered and injected into prompt."""

    async def test_skills_discovered_on_startup(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Skills in workspace/skills/ discovered during startup."""
        skills_dir = workspace / "skills"
        _write_skill(skills_dir, "code-review", "Review code for quality")
        _write_skill(skills_dir, "test-writer", "Write unit tests")
        _write_skill(skills_dir, "deploy", "Deploy application")
        # Skills are .md files directly in workspace/skills/

        agent = ArcAgent(config=agent_config)
        await agent.startup()

        # All 3 skills discovered
        assert len(agent.skills) == 3
        skill_names = {s.name for s in agent.skills}
        assert skill_names == {"code-review", "test-writer", "deploy"}

        await agent.shutdown()

    @patch("arcagent.core.agent.load_eval_model")
    @patch("arcagent.core.agent.arcrun_run")
    async def test_skills_injected_into_system_prompt(
        self,
        mock_arcrun_run: AsyncMock,
        mock_load_model: MagicMock,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Skill names appear in the system prompt via prompt injection."""
        skills_dir = workspace / "skills"
        _write_skill(skills_dir, "code-review", "Review code for quality")
        _write_skill(skills_dir, "test-writer", "Write unit tests")

        mock_load_model.return_value = MagicMock()
        mock_arcrun_run.return_value = MagicMock(content="done")

        agent = ArcAgent(config=agent_config)
        await agent.startup()

        await agent.run("test task")

        # System prompt should contain skill info
        prompt = mock_arcrun_run.call_args.kwargs["system_prompt"]
        assert "code-review" in prompt
        assert "test-writer" in prompt
        assert "Review code for quality" in prompt
        assert "Write unit tests" in prompt

        await agent.shutdown()

    async def test_skill_format_for_prompt_xml(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Skills formatted as XML in prompt."""
        skills_dir = workspace / "skills"
        _write_skill(skills_dir, "my-skill", "Does something useful")

        agent = ArcAgent(config=agent_config)
        await agent.startup()

        prompt_text = agent._skill_registry.format_for_prompt()
        assert "<available-skills>" in prompt_text
        assert '<skill name="my-skill">' in prompt_text
        assert "Does something useful" in prompt_text
        assert "</available-skills>" in prompt_text

        await agent.shutdown()

    async def test_skills_cached(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Skills are cached after discovery — same objects returned."""
        skills_dir = workspace / "skills"
        _write_skill(skills_dir, "cached-skill", "Test caching")

        agent = ArcAgent(config=agent_config)
        await agent.startup()

        first = agent.skills
        second = agent.skills
        assert first[0].name == second[0].name
        assert first[0].file_path == second[0].file_path

        await agent.shutdown()

    async def test_skills_cleared_on_shutdown(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Shutdown clears the skill cache."""
        skills_dir = workspace / "skills"
        _write_skill(skills_dir, "cleared-skill", "Will be cleared")

        agent = ArcAgent(config=agent_config)
        await agent.startup()
        assert len(agent.skills) == 1

        await agent.shutdown()
        assert len(agent.skills) == 0

    async def test_skills_rediscovered_on_reload(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Reload clears and re-discovers skills."""
        skills_dir = workspace / "skills"
        _write_skill(skills_dir, "initial-skill", "First version")

        agent = ArcAgent(config=agent_config)
        await agent.startup()
        assert len(agent.skills) == 1

        # Add another skill
        _write_skill(skills_dir, "new-skill", "Added after startup")

        await agent.reload()

        assert len(agent.skills) == 2
        skill_names = {s.name for s in agent.skills}
        assert "initial-skill" in skill_names
        assert "new-skill" in skill_names

        await agent.shutdown()

    async def test_malformed_skill_skipped(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Malformed SKILL.md files are skipped without crash."""
        skills_dir = workspace / "skills"

        # Good skill
        _write_skill(skills_dir, "good-skill", "This is good")

        # Malformed skill (invalid YAML) — directly in skills_dir
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "bad-skill.md").write_text("---\nname: [invalid yaml\n---\nBroken.\n")

        agent = ArcAgent(config=agent_config)
        await agent.startup()

        # Good skill still discovered, bad one skipped
        assert len(agent.skills) == 1
        assert agent.skills[0].name == "good-skill"

        await agent.shutdown()

    async def test_agent_created_skill_rescan(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Agent-created skills can be rescanned without full re-discovery."""
        # Start with no skills
        agent = ArcAgent(config=agent_config)
        await agent.startup()
        assert len(agent.skills) == 0

        # Agent creates a skill at runtime
        agent_dir = workspace / "skills" / "_agent-created"
        _write_skill(agent_dir, "runtime-skill", "Created at runtime")

        # Rescan just agent-created directory
        agent._skill_registry.rescan_agent_created(workspace)

        assert len(agent.skills) == 1
        assert agent.skills[0].name == "runtime-skill"

        await agent.shutdown()

    async def test_no_skills_is_safe(
        self,
        agent_config: ArcAgentConfig,
    ) -> None:
        """Agent starts fine with no skills directory."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        assert len(agent.skills) == 0
        assert agent._skill_registry.format_for_prompt() == ""

        await agent.shutdown()

    async def test_settings_accessible(
        self,
        agent_config: ArcAgentConfig,
    ) -> None:
        """Settings manager accessible after startup."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        assert agent.settings is not None
        # Can read settings
        model = agent.settings.get("model")
        assert model == "test/model"

        await agent.shutdown()
