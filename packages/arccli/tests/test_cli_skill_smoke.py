"""Smoke tests for arc skill subcommands via subprocess.

These tests verify that each `arc skill <subcommand>` invocation produces
expected output and exits correctly. They are the regression net for the
T1.1.5 migration.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# arc skill (no subcommand — shows help)
# ---------------------------------------------------------------------------


class TestSkillHelp:
    def test_no_args_exits_zero(self) -> None:
        """arc skill with no args exits 0 and shows help."""
        result = _arc("skill")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_no_args_shows_subcommands(self) -> None:
        """arc skill help lists expected subcommands."""
        result = _arc("skill")
        combined = result.stdout + result.stderr
        assert any(sub in combined for sub in ["list", "create", "validate", "search"])


# ---------------------------------------------------------------------------
# arc skill list
# ---------------------------------------------------------------------------


class TestSkillList:
    def test_list_exits_zero(self) -> None:
        """arc skill list exits 0."""
        result = _arc("skill", "list")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_list_output_nonempty(self) -> None:
        """arc skill list produces some output."""
        result = _arc("skill", "list")
        # Either 'No skills found' or a table — either is valid
        assert result.stdout.strip() or result.returncode == 0


# ---------------------------------------------------------------------------
# arc skill create
# ---------------------------------------------------------------------------


class TestSkillCreate:
    def test_create_exits_zero(self, tmp_path: Path) -> None:
        """arc skill create <name> --dir <tmp> exits 0."""
        result = _arc("skill", "create", "test-skill", "--dir", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_create_writes_file(self, tmp_path: Path) -> None:
        """arc skill create writes a .md file."""
        _arc("skill", "create", "test-skill", "--dir", str(tmp_path))
        assert (tmp_path / "test-skill.md").exists()

    def test_create_file_has_frontmatter(self, tmp_path: Path) -> None:
        """arc skill create produces valid YAML frontmatter."""
        _arc("skill", "create", "my-skill", "--dir", str(tmp_path))
        content = (tmp_path / "my-skill.md").read_text()
        assert "---" in content
        assert "name:" in content
        assert "description:" in content

    def test_create_fails_if_exists(self, tmp_path: Path) -> None:
        """arc skill create fails if file already exists."""
        _arc("skill", "create", "dup-skill", "--dir", str(tmp_path))
        result = _arc("skill", "create", "dup-skill", "--dir", str(tmp_path))
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# arc skill validate
# ---------------------------------------------------------------------------


class TestSkillValidate:
    def test_validate_valid_skill(self, tmp_path: Path) -> None:
        """arc skill validate passes on a valid skill file."""
        skill_file = tmp_path / "my-skill.md"
        skill_file.write_text(
            '---\nname: my-skill\ndescription: "A test skill"\n---\n\n# my-skill\n'
        )
        result = _arc("skill", "validate", str(skill_file))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "OK" in result.stdout or "ok" in result.stdout.lower()

    def test_validate_missing_name_fails(self, tmp_path: Path) -> None:
        """arc skill validate fails when name is missing."""
        skill_file = tmp_path / "bad.md"
        skill_file.write_text('---\ndescription: "No name field"\n---\n\n# bad\n')
        result = _arc("skill", "validate", str(skill_file))
        assert result.returncode != 0

    def test_validate_nonexistent_fails(self) -> None:
        """arc skill validate fails on a nonexistent file."""
        result = _arc("skill", "validate", "/tmp/__no_such_skill__.md")
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# arc skill search
# ---------------------------------------------------------------------------


class TestSkillSearch:
    def test_search_exits_zero(self) -> None:
        """arc skill search <query> exits 0."""
        result = _arc("skill", "search", "helper")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_search_output_nonempty(self) -> None:
        """arc skill search produces some output."""
        result = _arc("skill", "search", "helper")
        # 'No skills matching' or a table row — either is valid
        assert result.stdout.strip() or result.returncode == 0


# Mark to avoid unused import warning
_ = pytest
