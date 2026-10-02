"""J4 B1/B2 — frontmatter-only packs import; reject reasons name the file + rule.

* A third-party skill (frontmatter ``name`` + ``description``, free-form body)
  stages; each missing Arc section becomes a review finding instead of a refusal.
* A refusal names the failing file and the rule (``code: detail``) so the
  operator can fix the pack — never the bare ``skill failed validation``.
* Strict section mode (federal opt-in) refuses the same pack, with the reason.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.errors import CapabilityImportError
from arcagent.modules.capability_import.manifest import build_manifest
from arcagent.modules.capability_import.models import (
    CapabilityImportLimits,
    CapabilityImportManifest,
)

_FREE_FORM = (
    b"---\nname: pdf\ndescription: Work with PDF files.\n---\n# PDF\n\nSee references/a.md\n"
)


def _build(
    tmp_path: Path, entries: dict[str, bytes], *, strict: bool = False
) -> CapabilityImportManifest:
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for name, content in entries.items():
            handle.writestr(name, content)
    staged = intake(archive, tmp_path / "agent" / "capabilities")
    return build_manifest(
        staged.staging_dir,
        import_id=staged.import_id,
        target_agent_did="did:arc:agent:target",
        archive_sha256=staged.archive_sha256,
        limits=CapabilityImportLimits(),
        strict_sections=strict,
    )


def test_frontmatter_only_skill_stages_with_section_findings(tmp_path: Path) -> None:
    manifest = _build(
        tmp_path,
        {"skills/pdf/SKILL.md": _FREE_FORM, "skills/pdf/references/a.md": b"# a\n"},
    )

    assert manifest.skills == ("pdf",)
    finding = next(f for f in manifest.findings if f.startswith("missing_section:"))
    assert "skills/pdf/SKILL.md" in finding
    assert "## Steps" in finding


def test_rejection_names_the_file_and_the_rule(tmp_path: Path) -> None:
    bad = b"---\ndescription: no name here\n---\nbody\n"
    with pytest.raises(CapabilityImportError) as raised:
        _build(tmp_path, {"skills/broken/SKILL.md": bad})

    message = str(raised.value)
    assert "skills/broken/SKILL.md" in message
    assert "missing_frontmatter_field" in message
    assert "name" in message


def test_strict_sections_refuse_with_the_reason(tmp_path: Path) -> None:
    with pytest.raises(CapabilityImportError) as raised:
        _build(tmp_path, {"skills/pdf/SKILL.md": _FREE_FORM}, strict=True)

    message = str(raised.value)
    assert "skills/pdf/SKILL.md" in message
    assert "missing_section" in message


def test_tool_rejection_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(CapabilityImportError) as raised:
        _build(tmp_path, {"tools/evil.py": b"x = eval('1')\n"})

    assert "tools/evil.py" in str(raised.value)
