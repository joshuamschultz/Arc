"""Non-activating review service for staged capability imports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcagent.modules.capability_import.ledger import ImportLedger
from arcagent.modules.capability_import.manifest import (
    build_manifest,
    verify_manifest,
    write_evidence,
)
from arcagent.modules.capability_import.models import (
    CapabilityImportLimits,
    CapabilityImportManifest,
    CapabilityImportResult,
    CapabilityImportStatus,
)


class CapabilityImportService:
    """Create review evidence and track drift without signing or activation."""

    def __init__(self, capabilities_root: Path) -> None:
        self._root = Path(capabilities_root)
        self._ledger = ImportLedger(self._root)

    def list_reviews(self) -> list[dict[str, Any]]:
        """Return metadata-only review rows for this agent's staged imports.

        Staging content is executable code and remains quarantined. The UI gets
        only the signed review metadata and lifecycle state; it cannot turn a
        listing request into a source read or an activation.
        """
        staging_root = self._root / "imports" / ".staging"
        if not staging_root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for import_dir in sorted(staging_root.iterdir()):
            manifest_path = import_dir / "import.json"
            if not import_dir.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            row = dict(manifest)
            row["import_id"] = import_dir.name
            ledger = self._ledger.get(import_dir.name)
            row["status"] = str(ledger.get("status", "quarantined")) if ledger else "quarantined"
            rows.append(row)
        return rows

    def review(
        self,
        intake: CapabilityImportResult,
        *,
        target_agent_did: str,
        limits: CapabilityImportLimits,
    ) -> CapabilityImportManifest:
        manifest = build_manifest(
            intake.staging_dir,
            import_id=intake.import_id,
            target_agent_did=target_agent_did,
            archive_sha256=intake.archive_sha256,
            limits=limits,
        )
        write_evidence(intake.staging_dir, manifest)
        self._ledger.set(
            intake.import_id,
            CapabilityImportStatus.REVIEW_READY,
            review_digest=manifest.review_digest,
            target_agent_did=target_agent_did,
        )
        return manifest

    def status(
        self, manifest: CapabilityImportManifest, staging_dir: Path
    ) -> CapabilityImportStatus:
        """Return review state, marking the ledger modified when bytes drift."""
        if verify_manifest(manifest, staging_dir):
            row = self._ledger.get(manifest.import_id)
            if row is None:
                return CapabilityImportStatus.QUARANTINED
            return CapabilityImportStatus(str(row["status"]))
        self._ledger.set(manifest.import_id, CapabilityImportStatus.MODIFIED)
        return CapabilityImportStatus.MODIFIED
