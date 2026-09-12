"""SPEC-081 RED wave — open skill packages (REQ-398, REQ-401).

Covers:
- T-1051 (REQ-398): a ZIP with SKILL.md plus an arbitrary deep subtree under
  non-standard folder names imports successfully and every path survives
  normalization to ``skills/<name>/...``. RED today: ``intake`` rejects any path
  outside the fixed ``references/scripts/templates/assets`` allow-list with
  ``CapabilityImportLayoutError("source contains an unsupported path")``.
- T-1055 (REQ-401): regression guard — path-safety, resource limits, junk-drop,
  and ``CapabilityImportReviewFile.validate_path`` still hold under the looser
  layout, and a deep skill subpath serializes through review. The path-safety /
  limits / junk assertions are locks that already hold; the deep-subpath review
  serialization is RED today (it cannot be staged under the strict layout).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.errors import CapabilityImportError
from arcagent.modules.capability_import.manifest import build_manifest
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.service import CapabilityImportService

_TARGET_DID = "did:arc:agent:target"


def _skill_md(name: str = "myskill", description: str = "a rich imported skill") -> bytes:
    """A complete SKILL.md that passes both the layout gate and the content gate."""
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n"
        "## Files\nnone\n"
        "## Contract\nnone\n"
        "## Knowledge\nnone\n"
        "## Steps\nUse it.\n"
        "## Red Flags & Rationalizations\nnone\n"
        "## Validation\nnone\n"
        "## Examples\nnone\n"
    ).encode()


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


# --- T-1051 (REQ-398) ------------------------------------------------------


def test_intake_accepts_skill_with_arbitrary_deep_subtree(tmp_path: Path) -> None:
    """A skill-creator package (knowledge/, nested, non-standard dirs) imports.

    RED today: ``knowledge`` and ``x`` are not in the fixed resource-dir
    allow-list, so intake raises ``CapabilityImportLayoutError`` before staging.
    """
    archive = _zip(
        tmp_path / "rich-skill.zip",
        {
            "myskill/SKILL.md": _skill_md(),
            "myskill/knowledge/a/b.md": b"deep knowledge note",
            "myskill/x/y/z.txt": b"arbitrary nested content",
        },
    )

    result = intake(archive, tmp_path / "agent" / "capabilities")

    assert [entry.path for entry in result.files] == [
        "skills/myskill/SKILL.md",
        "skills/myskill/knowledge/a/b.md",
        "skills/myskill/x/y/z.txt",
    ]


def test_deep_subtree_skill_passes_the_content_gate_after_intake(tmp_path: Path) -> None:
    """The rich skill also survives ``build_manifest`` (the frontmatter/content gate).

    RED today: intake rejects the deep subtree, so the manifest is never built.
    """
    archive = _zip(
        tmp_path / "rich-skill.zip",
        {
            "myskill/SKILL.md": _skill_md(),
            "myskill/knowledge/a/b.md": b"deep knowledge note",
            "myskill/x/y/z.txt": b"arbitrary nested content",
        },
    )
    staged = intake(archive, tmp_path / "agent" / "capabilities")

    manifest = build_manifest(
        staged.staging_dir,
        import_id=staged.import_id,
        target_agent_did=_TARGET_DID,
        archive_sha256=staged.archive_sha256,
        limits=CapabilityImportLimits(),
    )

    assert manifest.skills == ("myskill",)
    assert "skills/myskill/knowledge/a/b.md" in {item.path for item in manifest.files}


# --- T-1055 (REQ-401): regression locks that must keep holding ------------


@pytest.mark.parametrize(
    "name",
    [
        "../skills/imported/escape.md",
        "/skills/imported/abs.md",
        "skills/imported/..\\evil.md",
        "skills/imported/nul\x00.md",
        "C:/skills/imported/colon.md",
        "skills\\imported\\back.md",
    ],
)
def test_path_safety_still_rejects_hostile_paths_under_looser_layout(
    tmp_path: Path, name: str
) -> None:
    archive = _zip(
        tmp_path / "hostile.zip",
        {"myskill/SKILL.md": _skill_md(), name: b"x"},
    )
    target = tmp_path / "agent" / "capabilities"

    with pytest.raises(CapabilityImportError):
        intake(archive, target)

    assert not (target / "imports" / ".staging").exists()


@pytest.mark.parametrize(
    "limits",
    [
        CapabilityImportLimits(max_files=1),
        CapabilityImportLimits(max_expanded_bytes=8),
        CapabilityImportLimits(max_file_bytes=8),
        CapabilityImportLimits(max_compression_ratio=1),
    ],
)
def test_resource_limits_still_enforced_under_looser_layout(
    tmp_path: Path, limits: CapabilityImportLimits
) -> None:
    archive = _zip(
        tmp_path / "big.zip",
        {
            "myskill/SKILL.md": _skill_md(),
            "myskill/knowledge/a/b.md": b"deep knowledge note that is not tiny",
        },
    )
    target = tmp_path / "agent" / "capabilities"

    with pytest.raises(CapabilityImportError):
        intake(archive, target, limits=limits)

    assert not (target / "imports" / ".staging").exists()


def test_review_file_path_check_still_rejects_out_of_root(tmp_path: Path) -> None:
    from arcagent.modules.capability_import.models import CapabilityImportReviewFile

    sha = "0" * 64
    with pytest.raises(ValueError):
        CapabilityImportReviewFile(path="secrets/leak.md", sha256=sha, size=1)
    with pytest.raises(ValueError):
        CapabilityImportReviewFile(path="../skills/imported/x.md", sha256=sha, size=1)


def test_deep_skill_subpath_serializes_through_review(tmp_path: Path) -> None:
    """A deep skill subpath survives intake -> manifest -> strict review wire model.

    RED today: the deep subtree cannot be staged under the strict layout, so the
    review contract is never exercised on a nested skill path.
    """
    archive = _zip(
        tmp_path / "rich-skill.zip",
        {
            "myskill/SKILL.md": _skill_md(),
            "myskill/knowledge/a/b.md": b"deep knowledge note",
        },
    )
    root = tmp_path / "agent" / "capabilities"
    staged = intake(archive, root)
    manifest = build_manifest(
        staged.staging_dir,
        import_id=staged.import_id,
        target_agent_did=_TARGET_DID,
        archive_sha256=staged.archive_sha256,
        limits=CapabilityImportLimits(),
    )

    review = CapabilityImportService(root).review_summary(manifest)

    assert "skills/myskill/knowledge/a/b.md" in {item.path for item in review.files}
