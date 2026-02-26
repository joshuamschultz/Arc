"""Tests for bio_memory CLI commands — read-only workspace inspection."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
import yaml
from click.testing import CliRunner

from arcagent.modules.bio_memory.cli import cli_group


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli(workspace: Path) -> click.Group:
    return cli_group(workspace)


def _write_episode(
    workspace: Path,
    name: str,
    frontmatter: dict[str, object],
    body: str,
) -> Path:
    memory_dir = workspace / "memory"
    episodes = memory_dir / "episodes"
    episodes.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.dump(frontmatter, default_flow_style=False).strip()
    content = f"---\n{fm_text}\n---\n\n{body}\n"
    path = episodes / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestStatus:
    """CLI 'status' command shows workspace overview."""

    def test_status_empty_workspace(
        self, runner: CliRunner, cli: click.Group,
    ) -> None:
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0

    def test_status_with_episodes(
        self, runner: CliRunner, cli: click.Group, workspace: Path,
    ) -> None:
        _write_episode(
            workspace, "ep1", {"tags": ["test"]}, "Episode 1",
        )
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "1" in result.output  # Should show episode count


class TestEpisodesList:
    """CLI 'episodes list' shows all episodes."""

    def test_no_episodes(
        self, runner: CliRunner, cli: click.Group,
    ) -> None:
        result = runner.invoke(cli, ["episodes", "list"])
        assert result.exit_code == 0

    def test_with_episodes(
        self, runner: CliRunner, cli: click.Group, workspace: Path,
    ) -> None:
        _write_episode(
            workspace, "2026-02-21-test",
            {"tags": ["test"], "title": "test"},
            "Test body",
        )
        result = runner.invoke(cli, ["episodes", "list"])
        assert result.exit_code == 0


class TestEntitiesList:
    """CLI 'entities list' shows all entity files."""

    def test_no_entities_dir(
        self, runner: CliRunner, cli: click.Group,
    ) -> None:
        result = runner.invoke(cli, ["entities", "list"])
        assert result.exit_code == 0

    def test_with_entities(
        self, runner: CliRunner, cli: click.Group, workspace: Path,
    ) -> None:
        entities_dir = workspace / "entities"
        entities_dir.mkdir()
        fm_text = yaml.dump(
            {"entity_type": "person", "name": "Josh", "status": "active"},
        ).strip()
        (entities_dir / "josh.md").write_text(
            f"---\n{fm_text}\n---\n\n# Josh\n", encoding="utf-8",
        )
        result = runner.invoke(cli, ["entities", "list"])
        assert result.exit_code == 0
        assert "Josh" in result.output


class TestEntitiesNormalize:
    """CLI 'entities normalize' adds frontmatter to legacy files."""

    def test_normalizes_legacy_file(
        self, runner: CliRunner, cli: click.Group, workspace: Path,
    ) -> None:
        entities_dir = workspace / "entities"
        entities_dir.mkdir()
        (entities_dir / "legacy.md").write_text("# Legacy\n\nContent.", encoding="utf-8")
        result = runner.invoke(cli, ["entities", "normalize"])
        assert result.exit_code == 0
        assert "Normalized 1" in result.output

    def test_no_entities_dir(
        self, runner: CliRunner, cli: click.Group,
    ) -> None:
        result = runner.invoke(cli, ["entities", "normalize"])
        assert result.exit_code == 0


class TestConsolidateDeep:
    """CLI 'consolidate-deep' command."""

    def test_runs_without_error(
        self, runner: CliRunner, cli: click.Group,
    ) -> None:
        result = runner.invoke(cli, ["consolidate-deep"])
        assert result.exit_code == 0


class TestWorkingShow:
    """CLI 'working show' displays working.md."""

    def test_no_working(
        self, runner: CliRunner, cli: click.Group,
    ) -> None:
        result = runner.invoke(cli, ["working", "show"])
        assert result.exit_code == 0

    def test_with_working(
        self, runner: CliRunner, cli: click.Group, workspace: Path,
    ) -> None:
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "working.md").write_text(
            "---\ntopics: [test]\n---\n\nActive task notes.",
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["working", "show"])
        assert result.exit_code == 0
        assert "Active task notes" in result.output
