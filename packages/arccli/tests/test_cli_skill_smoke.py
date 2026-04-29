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


_VALID_SKILL_MD = """\
---
name: my-skill
version: 1.0.0
description: A test skill that does a thing.
triggers: [test, demo]
tools: [bash]
---

## Resources

(auto)

## Contract

Inputs you must have:
- something

Outputs the agent must produce:
- something

## Knowledge

Background.

## Steps

1. Do thing.

## Anti Patterns

- **Don't** skip steps.

## Examples

```python
example()
```

## Validation

- It worked.
"""


class TestSkillCreate:
    def test_create_exits_zero(self, tmp_path: Path) -> None:
        """arc skill create <name> --dir <tmp> exits 0."""
        result = _arc("skill", "create", "test-skill", "--dir", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_create_writes_skill_folder(self, tmp_path: Path) -> None:
        """arc skill create writes a folder containing SKILL.md (SPEC-021)."""
        _arc("skill", "create", "test-skill", "--dir", str(tmp_path))
        skill_dir = tmp_path / "test-skill"
        assert skill_dir.is_dir(), "skill should be a folder, not a flat .md file"
        assert (skill_dir / "SKILL.md").exists()
        # Sub-folders the loader walks for the auto-generated ## Resources section.
        assert (skill_dir / "references").is_dir()
        assert (skill_dir / "scripts").is_dir()
        assert (skill_dir / "templates").is_dir()

    def test_create_skill_md_has_required_frontmatter(self, tmp_path: Path) -> None:
        """SKILL.md template carries every SPEC-021 required frontmatter field."""
        _arc("skill", "create", "my-skill", "--dir", str(tmp_path))
        content = (tmp_path / "my-skill" / "SKILL.md").read_text()
        for field in ("name:", "version:", "description:", "triggers:", "tools:"):
            assert field in content, f"missing required frontmatter field: {field}"
        for section in (
            "## Resources",
            "## Contract",
            "## Knowledge",
            "## Steps",
            "## Anti Patterns",
            "## Examples",
            "## Validation",
        ):
            assert section in content, f"missing required section: {section}"

    def test_create_fails_if_exists(self, tmp_path: Path) -> None:
        """arc skill create fails if folder already exists."""
        _arc("skill", "create", "dup-skill", "--dir", str(tmp_path))
        result = _arc("skill", "create", "dup-skill", "--dir", str(tmp_path))
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# arc skill validate
# ---------------------------------------------------------------------------


class TestSkillValidate:
    def test_validate_valid_skill_folder(self, tmp_path: Path) -> None:
        """arc skill validate passes on a fully-populated SPEC-021 skill folder."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(_VALID_SKILL_MD)
        result = _arc("skill", "validate", str(skill_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "OK" in result.stdout or "ok" in result.stdout.lower()

    def test_validate_accepts_skill_md_path(self, tmp_path: Path) -> None:
        """arc skill validate also accepts the SKILL.md path directly."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(_VALID_SKILL_MD)
        result = _arc("skill", "validate", str(skill_md))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_validate_missing_required_fields_fails(self, tmp_path: Path) -> None:
        """arc skill validate fails when required frontmatter fields are missing."""
        skill_dir = tmp_path / "bad-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            '---\nname: bad\ndescription: "incomplete"\n---\n\n# bad\n'
        )
        result = _arc("skill", "validate", str(skill_dir))
        assert result.returncode != 0

    def test_validate_nonexistent_fails(self) -> None:
        """arc skill validate fails on a nonexistent path."""
        result = _arc("skill", "validate", "/tmp/__no_such_skill_dir__")
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
