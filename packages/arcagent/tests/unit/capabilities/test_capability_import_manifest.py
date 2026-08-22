"""Deterministic review evidence tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.manifest import (
    build_manifest,
    cyclonedx_bom,
    verify_manifest,
)
from arcagent.modules.capability_import.models import CapabilityImportLimits


def test_manifest_binds_target_and_every_staged_file(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("tools/hello.py", "from arcagent import tool\n")
    staged = intake(source, tmp_path / "capabilities")
    manifest = build_manifest(
        staged.staging_dir,
        import_id=staged.import_id,
        target_agent_did="did:arc:agent:test",
        archive_sha256=staged.archive_sha256,
        limits=CapabilityImportLimits(),
    )

    assert manifest.target_agent_did == "did:arc:agent:test"
    assert manifest.files == staged.files
    assert cyclonedx_bom(manifest)["components"][0]["hashes"][0]["alg"] == "SHA-256"
    assert verify_manifest(manifest, staged.staging_dir)
