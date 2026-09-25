"""SPEC-081 Phase 3 RED wave — built-in name-collision review finding.

T-1069 (REQ-402, COMP-003): the loader already REFUSES a non-trusted skill that
shadows a trusted built-in name (capability_loader ``_register_skill_folder``
774-804). The missing half is *visibility before promote*: ``build_manifest`` /
``review`` must surface the collision as a review FINDING so an operator sees it
in the import review, not only as a silent refusal at load time.

The manifest runs on the staged tree and cannot know the agent's trusted
built-in names on its own, so this pins the seam: the caller passes the known
built-in names via a keyword-only ``reserved_skill_names`` argument to
``build_manifest``. When a staged skill's validated ``name`` is in that set, the
manifest carries a ``builtin_name_collision`` finding naming the skill; when it
is not, no such finding appears.

RED today: ``build_manifest`` has no ``reserved_skill_names`` parameter (the seam
does not exist), so the call raises ``TypeError`` — the feature is absent, not a
test typo. GREEN when the coder adds the seam and the finding.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.manifest import build_manifest
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.service import CapabilityImportService

_TARGET_DID = "did:arc:agent:target"

# A real TRUSTED built-in skill folder name (arcagent/builtins/capabilities/skills/).
# The wiring test below imports NO reserved names itself — it proves that
# CapabilityImportService.review resolves the real built-in names on its own.
_BUILTIN_SKILL_NAME = "create-skill"


def _skill_md(name: str) -> bytes:
    """A complete SKILL.md that passes both the layout gate and the content gate."""
    return (
        f"---\nname: {name}\ndescription: a skill under review\n---\n"
        "## Files\nnone\n"
        "## Contract\nnone\n"
        "## Knowledge\nnone\n"
        "## Steps\nUse it.\n"
        "## Red Flags & Rationalizations\nnone\n"
        "## Validation\nnone\n"
        "## Examples\nnone\n"
    ).encode()


def _stage_skill(tmp_path: Path, skill_name: str):
    """Intake a one-skill archive and return its staged import result."""
    archive = tmp_path / f"{skill_name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"skills/{skill_name}/SKILL.md", _skill_md(skill_name))
    return intake(archive, tmp_path / "agent" / "capabilities")


def test_manifest_surfaces_builtin_name_collision_as_review_finding(tmp_path: Path) -> None:
    """A staged skill whose name is a reserved built-in yields a collision finding."""
    staged = _stage_skill(tmp_path, "spec081_collider")

    manifest = build_manifest(
        staged.staging_dir,
        import_id=staged.import_id,
        target_agent_did=_TARGET_DID,
        archive_sha256=staged.archive_sha256,
        limits=CapabilityImportLimits(),
        reserved_skill_names=frozenset({"spec081_collider"}),
    )

    assert manifest.skills == ("spec081_collider",)
    assert any(
        "builtin_name_collision" in finding and "spec081_collider" in finding
        for finding in manifest.findings
    ), f"expected a builtin_name_collision finding naming the skill; got {manifest.findings!r}"


def test_manifest_has_no_collision_finding_when_name_is_not_reserved(tmp_path: Path) -> None:
    """A staged skill whose name collides with nothing carries no collision finding."""
    staged = _stage_skill(tmp_path, "spec081_unique_skill")

    manifest = build_manifest(
        staged.staging_dir,
        import_id=staged.import_id,
        target_agent_did=_TARGET_DID,
        archive_sha256=staged.archive_sha256,
        limits=CapabilityImportLimits(),
        reserved_skill_names=frozenset({"some_unrelated_builtin"}),
    )

    assert manifest.skills == ("spec081_unique_skill",)
    assert not any("builtin_name_collision" in finding for finding in manifest.findings), (
        f"expected no collision finding for a non-colliding skill; got {manifest.findings!r}"
    )


def test_review_surfaces_builtin_name_collision_for_real_builtin_skill(tmp_path: Path) -> None:
    """The REAL review path flags a staged skill shadowing a real built-in name.

    T-1069 wiring gap: ``build_manifest`` gained the ``reserved_skill_names`` seam,
    but ``CapabilityImportService.review`` must actually PASS the agent's trusted
    built-in skill names for the finding to fire in production. This drives review
    with NO reserved names of its own — it fails today because review passes none.
    """
    staged = _stage_skill(tmp_path, _BUILTIN_SKILL_NAME)
    service = CapabilityImportService(tmp_path / "agent" / "capabilities")

    manifest = service.review(
        staged,
        target_agent_did=_TARGET_DID,
        limits=CapabilityImportLimits(),
    )

    assert manifest.skills == (_BUILTIN_SKILL_NAME,)
    assert any(
        "builtin_name_collision" in finding and _BUILTIN_SKILL_NAME in finding
        for finding in manifest.findings
    ), (
        "expected review to surface a builtin_name_collision finding for a skill "
        f"shadowing built-in {_BUILTIN_SKILL_NAME!r}; got {manifest.findings!r}"
    )


def test_review_has_no_collision_finding_for_non_builtin_skill(tmp_path: Path) -> None:
    """A staged skill that shadows no built-in yields no collision finding via review."""
    staged = _stage_skill(tmp_path, "spec081_unique_skill")
    service = CapabilityImportService(tmp_path / "agent" / "capabilities")

    manifest = service.review(
        staged,
        target_agent_did=_TARGET_DID,
        limits=CapabilityImportLimits(),
    )

    assert manifest.skills == ("spec081_unique_skill",)
    assert not any("builtin_name_collision" in finding for finding in manifest.findings), (
        f"expected no collision finding for a non-colliding skill; got {manifest.findings!r}"
    )
