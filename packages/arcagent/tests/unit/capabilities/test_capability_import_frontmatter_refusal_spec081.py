"""SPEC-081 RED wave — refuse skills with no name/description (REQ-399).

T-1056: on the IMPORT path (``service.review`` -> ``manifest.build_manifest`` ->
``_validate_skills`` -> ``validate_skill_folder``), a skill whose SKILL.md is
missing or has a blank ``name`` or ``description`` MUST be refused (raise), never
silently accepted and staged as a valid skill.

These packages are laid out so intake succeeds (valid layout, minimal shape) and
the refusal — if any — must come from the content gate, isolating the
frontmatter contract from the layout gate.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.errors import CapabilityImportError
from arcagent.modules.capability_import.manifest import build_manifest
from arcagent.modules.capability_import.models import CapabilityImportLimits

_SECTIONS = (
    "## Files\nnone\n## Contract\nnone\n## Knowledge\nnone\n"
    "## Steps\nUse it.\n## Red Flags & Rationalizations\nnone\n"
    "## Validation\nnone\n## Examples\nnone\n"
)

_MISSING_NAME = f"---\ndescription: has a description only\n---\n{_SECTIONS}".encode()
_BLANK_NAME = f'---\nname: ""\ndescription: has a description\n---\n{_SECTIONS}'.encode()
_MISSING_DESCRIPTION = f"---\nname: imported\n---\n{_SECTIONS}".encode()
_BLANK_DESCRIPTION = f'---\nname: imported\ndescription: ""\n---\n{_SECTIONS}'.encode()


def _build(tmp_path: Path, entries: dict[str, bytes]) -> None:
    archive = tmp_path / "bad-skill.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for name, content in entries.items():
            handle.writestr(name, content)
    staged = intake(archive, tmp_path / "agent" / "capabilities")
    build_manifest(
        staged.staging_dir,
        import_id=staged.import_id,
        target_agent_did="did:arc:agent:target",
        archive_sha256=staged.archive_sha256,
        limits=CapabilityImportLimits(),
    )


@pytest.mark.parametrize(
    "skill_md",
    [_MISSING_NAME, _BLANK_NAME, _MISSING_DESCRIPTION, _BLANK_DESCRIPTION],
)
def test_import_refuses_skill_without_name_or_description(
    tmp_path: Path, skill_md: bytes
) -> None:
    with pytest.raises(CapabilityImportError):
        _build(tmp_path, {"skills/imported/SKILL.md": skill_md})


def test_import_refuses_skill_folder_with_no_skill_md(tmp_path: Path) -> None:
    with pytest.raises(CapabilityImportError):
        _build(tmp_path, {"skills/imported/references/guide.md": b"orphan resource, no SKILL.md"})
