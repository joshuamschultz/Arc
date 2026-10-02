"""SPEC-021 Task 2.8 — skill folder validator tests.

Verifies frontmatter, section, filler, and tool-dependency checks.
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
        from arcagent.capabilities.skill_validator import validate_skill_folder

        result = validate_skill_folder(tmp_path / "no-skill", "builtins")
        assert any(e.code == "missing_skill_md" for e in result.errors)

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

        folder = tmp_path / "broken"
        folder.mkdir()
        (folder / "SKILL.md").write_text("just text\n")
        result = validate_skill_folder(folder, "builtins")
        assert any(e.code == "malformed_frontmatter" for e in result.errors)
        # A genuinely missing block says exactly that.
        assert any("no leading --- frontmatter block" in e.detail for e in result.errors)

    def test_invalid_yaml_frontmatter_reports_yaml_error_not_missing_block(
        self, tmp_path: Path
    ) -> None:
        """A leading --- block WITH invalid YAML (an unquoted colon in a value —
        the common real case) must report the YAML error, not the misleading
        'no leading --- frontmatter block' — the block is right there."""
        from arcagent.capabilities.skill_validator import validate_skill_folder

        folder = tmp_path / "skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\n"
            "name: x\n"
            "version: 1.0.0\n"
            "description: Do the thing. TRIGGER: when asked. SKIP: otherwise.\n"
            "triggers: [a]\n"
            "tools: [read]\n"
            "---\n\n## Resources\n"
        )
        result = validate_skill_folder(folder, "builtins")
        malformed = [e for e in result.errors if e.code == "malformed_frontmatter"]
        assert malformed, "invalid YAML should still be a malformed_frontmatter error"
        assert any("invalid YAML frontmatter" in e.detail for e in malformed)
        assert not any("no leading --- frontmatter block" in e.detail for e in malformed)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

        folder = tmp_path / "skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\n"
            "name: x\n"
            "version: 1.0.0\n"
            # description: missing (a genuinely required field)
            "triggers: [a]\n"
            "tools: [read]\n"
            "---\n"
            "\n## Resources\n## Contract\n## Knowledge\n"
            "## Steps\n## Anti Patterns\n## Examples\n## Validation\n"
        )
        result = validate_skill_folder(folder, "builtins")
        assert any(e.code == "missing_frontmatter_field" for e in result.errors)
        assert "description" in result.errors[0].detail

    def test_triggers_and_tools_are_optional(self, tmp_path: Path) -> None:
        """A skill-creator v2 skill omits triggers/tools; it must still load."""
        from arcagent.capabilities.skill_validator import validate_skill_folder

        folder = tmp_path / "skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\n"
            "name: v2-skill\n"
            'description: "does a thing. TRIGGER: when asked. SKIP: otherwise."\n'
            "---\n"
            "\n## Files\n\n## Contract\n\n## Knowledge\n\n## Steps\n"
            "do x\n## Output\n\n## Red Flags & Rationalizations\n\n"
            "## Validation\n\n## Examples\n"
        )
        result = validate_skill_folder(folder, "builtins")
        assert result.ok, result.errors
        assert result.entry is not None
        assert result.entry.name == "v2-skill"
        assert result.entry.triggers == ()
        assert result.entry.tools == ()
        assert result.entry.version == "0.0.0"


class TestSectionValidation:
    """Only frontmatter ``name`` + ``description`` are required (J4 B1, Q36).

    The Arc section groups are review findings (warnings) by default so a
    third-party pack (Anthropic / skills.sh / skill-creator) loads; strict mode
    turns them back into errors and is an operator opt-in.
    """

    _MISSING_STEPS = (
        "## Resources\n## Contract\n## Knowledge\n"
        # Missing ## Steps
        "## Anti Patterns\n## Examples\n## Validation\n"
    )

    def test_missing_section_is_a_warning_by_default(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

        _write_skill(tmp_path / "skill", body=self._MISSING_STEPS)
        result = validate_skill_folder(tmp_path / "skill", "import")
        assert result.ok
        assert result.entry is not None
        warning = next(w for w in result.warnings if w.code == "missing_section")
        assert "Steps" in warning.detail

    def test_missing_section_rejected_in_strict_mode(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

        _write_skill(tmp_path / "skill", body=self._MISSING_STEPS)
        result = validate_skill_folder(tmp_path / "skill", "import", strict_sections=True)
        assert any(e.code == "missing_section" for e in result.errors)
        assert "Steps" in result.errors[0].detail

    def test_frontmatter_only_third_party_skill_is_valid(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

        folder = tmp_path / "pdf"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: Work with PDF files.\nlicense: see LICENSE.txt\n---\n"
            "# PDF processing\n\nFree-form body. See references/advanced.md.\n"
        )
        result = validate_skill_folder(folder, "import")
        assert result.ok, result.errors
        assert result.entry is not None and result.entry.name == "pdf"
        assert {w.code for w in result.warnings} == {"missing_section"}


class TestStrictSectionsForTier:
    def test_strict_only_at_federal_and_only_by_config(self) -> None:
        from arcagent.capabilities.skill_validator import strict_sections_for

        assert strict_sections_for("federal", configured=True) is True
        assert strict_sections_for("federal", configured=False) is False
        assert strict_sections_for("enterprise", configured=True) is False
        assert strict_sections_for("personal", configured=True) is False


class TestCompleteSkill:
    def test_complete_skill_ok(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

        _write_skill(tmp_path / "skill")
        result = validate_skill_folder(tmp_path / "skill", "builtins")
        assert result.ok
        assert result.entry is not None
        assert result.entry.name == "my-skill"


class TestFillerDetection:
    def test_na_section_warns(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

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
        from arcagent.capabilities.skill_validator import validate_skill_folder

        # Default body has empty Resources; should not warn.
        _write_skill(tmp_path / "skill")
        result = validate_skill_folder(tmp_path / "skill", "builtins")
        assert not any(
            w.code == "filler_section" and "Resources" in w.detail for w in result.warnings
        )


class TestToolDependencyCheck:
    def test_missing_tool_warns(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

        _write_skill(tmp_path / "skill")
        result = validate_skill_folder(
            tmp_path / "skill",
            "builtins",
            known_tools={"read"},  # write declared but not registered
        )
        assert any(w.code == "tool_dependency_policy_denied" for w in result.warnings)
        assert "write" in result.warnings[-1].detail

    def test_all_tools_present_no_warning(self, tmp_path: Path) -> None:
        from arcagent.capabilities.skill_validator import validate_skill_folder

        _write_skill(tmp_path / "skill")
        result = validate_skill_folder(
            tmp_path / "skill",
            "builtins",
            known_tools={"read", "write"},
        )
        assert not any(w.code == "tool_dependency_policy_denied" for w in result.warnings)


class TestShippedBuiltinsStillValidate:
    """The reconciled contract must not break Arc's own shipped skills."""

    def test_all_builtin_skills_validate(self) -> None:
        import arcagent
        from arcagent.capabilities.skill_validator import validate_skill_folder

        skills_root = Path(arcagent.__file__).parent / "builtins" / "capabilities" / "skills"
        folders = [p for p in skills_root.iterdir() if (p / "SKILL.md").is_file()]
        assert folders, f"no builtin skills found under {skills_root}"
        for folder in folders:
            result = validate_skill_folder(folder, "builtins")
            assert result.ok, f"{folder.name}: {[e.detail for e in result.errors]}"
