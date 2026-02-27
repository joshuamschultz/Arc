"""Tests for the skill registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.skill_registry import SkillRegistry


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def global_dir(tmp_path: Path) -> Path:
    gd = tmp_path / "global"
    gd.mkdir()
    return gd


def _write_skill(directory: Path, filename: str, name: str, description: str) -> Path:
    """Helper to write a SKILL.md file with YAML frontmatter."""
    path = directory / filename
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nFull content here.\n"
    )
    return path


class TestSkillDiscovery:
    """Skill discovery from workspace and global directories."""

    def test_discover_from_workspace_skills(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        _write_skill(skills_dir, "review.md", "code-review", "Review code quality")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 1
        assert registry.skills[0].name == "code-review"

    def test_discover_from_global_dir(self, workspace: Path, global_dir: Path) -> None:
        _write_skill(global_dir, "debug.md", "debug", "Debug issues")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 1
        assert registry.skills[0].name == "debug"

    def test_discover_from_agent_created(self, workspace: Path, global_dir: Path) -> None:
        created_dir = workspace / "skills" / "_agent-created"
        created_dir.mkdir(parents=True)
        _write_skill(created_dir, "custom.md", "custom-tool", "Agent-created skill")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 1
        assert registry.skills[0].name == "custom-tool"

    def test_discover_multiple_sources(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        _write_skill(skills_dir, "a.md", "skill-a", "Skill A")
        _write_skill(global_dir, "b.md", "skill-b", "Skill B")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        names = {s.name for s in registry.skills}
        assert names == {"skill-a", "skill-b"}

    def test_discover_skips_non_md_files(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        (skills_dir / "notes.txt").write_text("not a skill")
        _write_skill(skills_dir, "real.md", "real", "Real skill")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 1

    def test_discover_empty_directories(self, workspace: Path, global_dir: Path) -> None:
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 0

    def test_discover_nonexistent_directories(self, tmp_path: Path) -> None:
        registry = SkillRegistry()
        registry.discover(tmp_path / "nope", tmp_path / "also_nope")
        assert len(registry.skills) == 0


class TestFrontmatterParsing:
    """YAML frontmatter parsing tests."""

    def test_parse_all_fields(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        (skills_dir / "full.md").write_text(
            "---\n"
            "name: full-skill\n"
            "description: A full skill\n"
            "version: 1.2.0\n"
            "author: test-author\n"
            "requires:\n  - read\n  - grep\n"
            "tags:\n  - quality\n  - security\n"
            "category: development\n"
            "---\n\n# Full Skill\n"
        )
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        skill = registry.skills[0]
        assert skill.name == "full-skill"
        assert skill.description == "A full skill"
        assert skill.version == "1.2.0"
        assert skill.author == "test-author"
        assert skill.requires == ["read", "grep"]
        assert skill.tags == ["quality", "security"]
        assert skill.category == "development"

    def test_skip_missing_required_name(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        (skills_dir / "bad.md").write_text("---\ndescription: No name field\n---\n\nContent\n")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 0

    def test_skip_missing_required_description(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        (skills_dir / "bad.md").write_text("---\nname: no-desc\n---\n\nContent\n")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 0

    def test_skip_malformed_yaml(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        (skills_dir / "bad.md").write_text("---\nname: [invalid yaml\n---\n\nContent\n")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 0

    def test_skip_no_frontmatter(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        (skills_dir / "plain.md").write_text("# Just markdown\n\nNo frontmatter.\n")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 0

    def test_file_path_stored(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        path = _write_skill(skills_dir, "tracked.md", "tracked", "Has path")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert registry.skills[0].file_path == path


class TestFormatForPrompt:
    """Prompt formatting tests."""

    def test_format_xml(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        _write_skill(skills_dir, "a.md", "skill-a", "Does A")
        _write_skill(skills_dir, "b.md", "skill-b", "Does B")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        result = registry.format_for_prompt()
        assert "<available-skills>" in result
        assert "</available-skills>" in result
        assert '<skill name="skill-a">Does A</skill>' in result
        assert '<skill name="skill-b">Does B</skill>' in result

    def test_format_empty(self) -> None:
        registry = SkillRegistry()
        assert registry.format_for_prompt() == ""


class TestSkillCache:
    """Cache behavior tests."""

    def test_clear_resets_skills(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        _write_skill(skills_dir, "a.md", "skill-a", "Does A")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 1
        registry.clear()
        assert len(registry.skills) == 0

    def test_get_skill_by_name(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        _write_skill(skills_dir, "target.md", "target", "Target skill")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        skill = registry.get_skill("target")
        assert skill is not None
        assert skill.name == "target"

    def test_get_nonexistent_skill(self) -> None:
        registry = SkillRegistry()
        assert registry.get_skill("nope") is None


class TestAgentCreatedRescan:
    """Targeted re-scan of agent-created skills."""

    def test_rescan_adds_new_skill(self, workspace: Path, global_dir: Path) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        _write_skill(skills_dir, "existing.md", "existing", "Existing skill")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        assert len(registry.skills) == 1

        # Agent creates a new skill
        created_dir = workspace / "skills" / "_agent-created"
        created_dir.mkdir()
        _write_skill(created_dir, "new.md", "new-skill", "Newly created")
        registry.rescan_agent_created(workspace)
        assert len(registry.skills) == 2
        names = {s.name for s in registry.skills}
        assert "new-skill" in names


class TestSkillEdgeCases:
    """Edge cases and error handling."""

    def test_parse_skill_file_read_error(self, workspace: Path, global_dir: Path) -> None:
        """Lines 117-119: OSError when reading skill file."""
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        # Create a file we can't read (simulate permission issue)
        skill_file = skills_dir / "unreadable.md"
        skill_file.write_text("---\nname: test\ndescription: Test\n---\n")

        registry = SkillRegistry()
        # Should discover successfully
        registry.discover(workspace, global_dir)
        # At least shouldn't crash

    def test_frontmatter_not_dict(self, workspace: Path, global_dir: Path) -> None:
        """Lines 132-133: YAML frontmatter is not a dict."""
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        (skills_dir / "list.md").write_text("---\n- item1\n- item2\n---\n\nContent\n")
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        # Should skip the invalid frontmatter
        assert len(registry.skills) == 0

    def test_extract_frontmatter_no_closing_delimiter(
        self, workspace: Path, global_dir: Path
    ) -> None:
        """Line 160: No closing --- delimiter."""
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        (skills_dir / "unclosed.md").write_text(
            "---\nname: test\ndescription: Test\n\nNo closing delimiter"
        )
        registry = SkillRegistry()
        registry.discover(workspace, global_dir)
        # Should skip files without proper frontmatter
        assert len(registry.skills) == 0

    def test_format_for_prompt_edge_case(self, workspace: Path, global_dir: Path) -> None:
        """Test format_for_prompt with various skill configurations."""
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        _write_skill(skills_dir, "simple.md", "simple", "Simple skill")

        registry = SkillRegistry()
        registry.discover(workspace, global_dir)

        # Format all skills
        formatted = registry.format_for_prompt()
        assert "simple" in formatted
        assert "Simple skill" in formatted


class TestSkillFileReadError:
    """Lines 117-119: OSError reading skill file is handled."""

    def test_oserror_reading_skill_file_skipped(
        self, workspace: Path, global_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        skills_dir = workspace / "skills"
        skills_dir.mkdir()
        _write_skill(skills_dir, "good.md", "good", "Good skill")
        bad = skills_dir / "bad.md"
        bad.write_text("---\nname: bad\n---\n# Bad")
        bad.chmod(0o000)

        registry = SkillRegistry()
        registry.discover(workspace, global_dir)

        # Restore permissions for cleanup
        bad.chmod(0o644)

        # Good skill should still be found
        assert registry.get_skill("good") is not None
