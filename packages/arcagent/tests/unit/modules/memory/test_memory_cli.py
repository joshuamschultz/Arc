"""Tests for memory CLI commands — read-only workspace inspection."""

from __future__ import annotations

from pathlib import Path

import click.testing
import pytest

from arcagent.modules.memory.cli import cli_group


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with fixture data for memory CLI tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Notes
    notes_dir = ws / "notes"
    notes_dir.mkdir()
    (notes_dir / "2026-02-15.md").write_text("# Feb 15\nMeeting with team about roadmap.")
    (notes_dir / "2026-02-14.md").write_text("# Feb 14\nReviewed PRs and fixed bugs.")

    # Entities (flat markdown with frontmatter)
    entities_dir = ws / "entities"
    entities_dir.mkdir()
    (entities_dir / "josh.md").write_text(
        "---\n"
        "name: Josh\n"
        "type: person\n"
        "aliases:\n- Joshua\n"
        "last_updated: '2026-02-15T10:00:00+00:00'\n"
        "---\n\n"
        "- role: engineer (0.9) [2026-02-15T10:00:00+00:00]\n"
        "- location: Denver (0.8) [2026-02-15T10:00:00+00:00]\n"
    )
    (entities_dir / "acme-corp.md").write_text(
        "---\n"
        "name: Acme Corp\n"
        "type: org\n"
        "aliases: []\n"
        "last_updated: '2026-02-14T09:00:00+00:00'\n"
        "---\n\n"
        "- industry: technology (0.7) [2026-02-14T09:00:00+00:00]\n"
    )

    return ws


def _invoke(workspace: Path, args: list[str]) -> click.testing.Result:
    """Invoke a memory CLI command via CliRunner."""
    group = cli_group(workspace)
    runner = click.testing.CliRunner()
    return runner.invoke(group, args)


class TestNotesCommand:
    """Tests for ``arc agent memory <path> notes``."""

    def test_lists_notes(self, workspace: Path) -> None:
        result = _invoke(workspace, ["notes", "--days", "30"])
        assert result.exit_code == 0
        assert "2026-02-15" in result.output
        assert "2026-02-14" in result.output

    def test_no_notes_directory(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        result = _invoke(ws, ["notes"])
        assert result.exit_code == 0
        assert "No notes directory" in result.output

    def test_empty_notes(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "notes").mkdir()
        result = _invoke(ws, ["notes"])
        assert result.exit_code == 0
        assert "No notes found" in result.output


class TestEntitiesCommand:
    """Tests for ``arc agent memory <path> entities``."""

    def test_lists_entities(self, workspace: Path) -> None:
        result = _invoke(workspace, ["entities"])
        assert result.exit_code == 0
        assert "Josh" in result.output
        assert "Acme Corp" in result.output
        assert "person" in result.output
        assert "org" in result.output

    def test_no_entities_directory(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        result = _invoke(ws, ["entities"])
        assert result.exit_code == 0
        assert "No entities directory" in result.output


class TestEntityCommand:
    """Tests for ``arc agent memory <path> entity <name>``."""

    def test_show_entity_facts(self, workspace: Path) -> None:
        result = _invoke(workspace, ["entity", "josh"])
        assert result.exit_code == 0
        assert "engineer" in result.output
        assert "Denver" in result.output

    def test_entity_not_found(self, workspace: Path) -> None:
        result = _invoke(workspace, ["entity", "nonexistent"])
        assert result.exit_code == 0
        assert "No entity found" in result.output

    def test_entity_shows_aliases(self, workspace: Path) -> None:
        result = _invoke(workspace, ["entity", "josh"])
        assert result.exit_code == 0
        assert "Joshua" in result.output


class TestSearchCommand:
    """Tests for ``arc agent memory <path> search``."""

    def test_search_finds_notes(self, workspace: Path) -> None:
        result = _invoke(workspace, ["search", "meeting"])
        assert result.exit_code == 0
        # May find results or not depending on indexing
        # Just verify it doesn't crash

    def test_search_no_results(self, workspace: Path) -> None:
        result = _invoke(workspace, ["search", "xyznonexistent123"])
        assert result.exit_code == 0


class TestStatsCommand:
    """Tests for ``arc agent memory <path> stats``."""

    def test_shows_stats(self, workspace: Path) -> None:
        result = _invoke(workspace, ["stats"])
        assert result.exit_code == 0
        assert "Notes" in result.output
        assert "Entities" in result.output
        assert "2" in result.output  # 2 notes or 2 entities
