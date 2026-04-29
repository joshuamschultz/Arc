"""SPEC-021 Task 2.8 — skill folder validator tests.

Verifies frontmatter, section, filler, and tool-dependency checks.
Also verifies the auto-generated ``## Resources`` section reflects
folder contents (R-013).
"""

from __future__ import annotations

from pathlib import Path


def _write_skill(folder: Path, *, body: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    default_body = (
        "## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n"
        "follow these steps\n## Anti Patterns\n\n## Examples\n\n"
        "## Validation\n"
    )
    text = (
        "---\n"
        "name: my-skill\n"
        "version: 1.0.0\n"
        "description: a thing\n"
        "triggers: [a, b]\n"
        "tools: [read, write]\n"
        "---\n"
        "\n"
    ) + (body or default_body)
    (folder / "SKILL.md").write_text(text)
    return folder


class TestFrontmatterValidation:
    def test_missing_skill_md(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import validate_skill_folder

        result = validate_skill_folder(tmp_path / "no-skill", "builtins")
        assert any(e.code == "missing_skill_md" for e in result.errors)

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import validate_skill_folder

        folder = tmp_path / "broken"
        folder.mkdir()
        (folder / "SKILL.md").write_text("just text\n")
        result = validate_skill_folder(folder, "builtins")
        assert any(e.code == "malformed_frontmatter" for e in result.errors)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import validate_skill_folder

        folder = tmp_path / "skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\n"
            "name: x\n"
            "version: 1.0.0\n"
            "description: y\n"
            "triggers: [a]\n"
            # tools: missing
            "---\n"
            "\n## Resources\n## Contract\n## Knowledge\n"
            "## Steps\n## Anti Patterns\n## Examples\n## Validation\n"
        )
        result = validate_skill_folder(folder, "builtins")
        assert any(e.code == "missing_frontmatter_field" for e in result.errors)
        assert "tools" in result.errors[0].detail


class TestSectionValidation:
    def test_missing_section_rejected(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import validate_skill_folder

        bad_body = (
            "## Resources\n## Contract\n## Knowledge\n"
            # Missing ## Steps
            "## Anti Patterns\n## Examples\n## Validation\n"
        )
        _write_skill(tmp_path / "skill", body=bad_body)
        result = validate_skill_folder(tmp_path / "skill", "builtins")
        assert any(e.code == "missing_section" for e in result.errors)
        assert "Steps" in result.errors[0].detail

    def test_complete_skill_ok(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import validate_skill_folder

        _write_skill(tmp_path / "skill")
        result = validate_skill_folder(tmp_path / "skill", "builtins")
        assert result.ok
        assert result.entry is not None
        assert result.entry.name == "my-skill"


class TestFillerDetection:
    def test_na_section_warns(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import validate_skill_folder

        bad_body = (
            "## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n"
            "do x\n## Anti Patterns\nN/A\n## Examples\n\n## Validation\n"
        )
        _write_skill(tmp_path / "skill", body=bad_body)
        result = validate_skill_folder(tmp_path / "skill", "builtins")
        assert result.ok  # filler is warning, not error
        assert any(w.code == "filler_section" for w in result.warnings)

    def test_resources_filler_ignored(self, tmp_path: Path) -> None:
        """``## Resources`` is auto-filled — empty there is fine."""
        from arcagent.core.skill_validator import validate_skill_folder

        # Default body has empty Resources; should not warn.
        _write_skill(tmp_path / "skill")
        result = validate_skill_folder(tmp_path / "skill", "builtins")
        assert not any(
            w.code == "filler_section" and "Resources" in w.detail for w in result.warnings
        )


class TestToolDependencyCheck:
    def test_missing_tool_warns(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import validate_skill_folder

        _write_skill(tmp_path / "skill")
        result = validate_skill_folder(
            tmp_path / "skill",
            "builtins",
            known_tools={"read"},  # write declared but not registered
        )
        assert any(w.code == "tool_dependency_policy_denied" for w in result.warnings)
        assert "write" in result.warnings[-1].detail

    def test_all_tools_present_no_warning(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import validate_skill_folder

        _write_skill(tmp_path / "skill")
        result = validate_skill_folder(
            tmp_path / "skill",
            "builtins",
            known_tools={"read", "write"},
        )
        assert not any(w.code == "tool_dependency_policy_denied" for w in result.warnings)


class TestRenderResourcesSection:
    def test_empty_folder(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import render_resources_section

        folder = tmp_path / "empty"
        folder.mkdir()
        rendered = render_resources_section(folder)
        assert rendered.startswith("## Resources")
        assert "(no resources)" in rendered

    def test_full_folder_listing(self, tmp_path: Path) -> None:
        from arcagent.core.skill_validator import render_resources_section

        folder = tmp_path / "skill"
        folder.mkdir()
        (folder / "references").mkdir()
        (folder / "references/decorator-fields.md").write_text("x")
        (folder / "scripts").mkdir()
        (folder / "scripts/validate.py").write_text("x")
        rendered = render_resources_section(folder)
        assert "**references/**" in rendered
        assert "decorator-fields.md" in rendered
        assert "**scripts/**" in rendered
        assert "validate.py" in rendered
