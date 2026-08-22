"""Review evidence must fail closed when staged content changes."""

from __future__ import annotations

import zipfile
from pathlib import Path

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import (
    CapabilityImportLimits,
    CapabilityImportStatus,
)
from arcagent.modules.capability_import.service import CapabilityImportService


def test_changed_staged_byte_marks_import_modified(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("tools/hello.py", "from arcagent import tool\n")
    staged = intake(source, tmp_path / "capabilities")
    service = CapabilityImportService(tmp_path / "capabilities")
    manifest = service.review(
        staged, target_agent_did="did:arc:agent:test", limits=CapabilityImportLimits()
    )
    (staged.staging_dir / "tools" / "hello.py").write_text("changed", encoding="utf-8")

    assert service.status(manifest, staged.staging_dir) is CapabilityImportStatus.MODIFIED
