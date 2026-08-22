"""Non-activating review service for staged capability imports."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

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
    CapabilityImportReview,
    CapabilityImportStatus,
)

_MAX_REVIEW_BYTES = 1024 * 1024
_MANIFEST_FIELDS = frozenset(
    {
        "archive_sha256",
        "files",
        "findings",
        "import_id",
        "limits",
        "review_digest",
        "skills",
        "supplier_metadata",
        "supplier_sbom_sha256",
        "target_agent_did",
        "tools",
    }
)


class CapabilityImportService:
    """Create review evidence and track drift without signing or activation."""

    def __init__(self, capabilities_root: Path) -> None:
        self._root = Path(capabilities_root)
        self._ledger = ImportLedger(self._root)

    def list_reviews(self) -> list[CapabilityImportReview]:
        """Return metadata-only review rows for this agent's staged imports.

        Staging content is executable code and remains quarantined. The UI gets
        only the signed review metadata and lifecycle state; it cannot turn a
        listing request into a source read or an activation.
        """
        staging_root = self._root / "imports" / ".staging"
        if not staging_root.is_dir():
            return []
        rows: list[CapabilityImportReview] = []
        try:
            import_dirs = sorted(staging_root.iterdir())
        except OSError:
            return rows
        for import_dir in import_dirs:
            manifest_path = import_dir / "import.json"
            if not import_dir.is_dir() or not manifest_path.is_file():
                continue
            try:
                if manifest_path.stat().st_size > _MAX_REVIEW_BYTES:
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            if manifest.get("import_id") != import_dir.name:
                continue
            if not set(manifest).issubset(_MANIFEST_FIELDS):
                continue
            supplier = manifest.get("supplier_metadata")
            if not isinstance(supplier, dict):
                continue
            try:
                ledger = self._ledger.get(import_dir.name)
                if ledger is not None and not isinstance(ledger, dict):
                    continue
                status = CapabilityImportStatus(
                    str(ledger.get("status", CapabilityImportStatus.QUARANTINED.value))
                    if ledger
                    else CapabilityImportStatus.QUARANTINED.value
                )
                rows.append(self._review_from_payload(manifest, status=status))
            except (RuntimeError, TypeError, ValueError, ValidationError):
                continue
        return rows

    def review_summary(
        self,
        manifest: CapabilityImportManifest,
        *,
        status: CapabilityImportStatus = CapabilityImportStatus.REVIEW_READY,
    ) -> CapabilityImportReview:
        """Return the strict metadata contract for a freshly reviewed import."""
        return self._review_from_payload(manifest.__dict__, status=status)

    @staticmethod
    def _review_from_payload(
        payload: dict[str, object], *, status: CapabilityImportStatus
    ) -> CapabilityImportReview:
        supplier = payload.get("supplier_metadata")
        if not isinstance(supplier, dict):
            raise ValueError("supplier metadata must be an object")
        files = payload.get("files")
        if not isinstance(files, (list, tuple)):
            raise ValueError("review files must be a sequence")
        tools = payload.get("tools")
        skills = payload.get("skills")
        if not isinstance(tools, (list, tuple)) or not isinstance(skills, (list, tuple)):
            raise ValueError("review capabilities must be sequences")
        normalized = {
            "import_id": payload.get("import_id"),
            "status": status,
            "target_agent_did": payload.get("target_agent_did"),
            "archive_sha256": payload.get("archive_sha256"),
            "review_digest": payload.get("review_digest"),
            "files": [item.__dict__ if hasattr(item, "__dict__") else item for item in files],
            "tools": list(tools),
            "skills": list(skills),
            "supplier_sbom_sha256": payload.get("supplier_sbom_sha256"),
            "supplier_metadata_keys": list(supplier),
            "activation": "review_only",
        }
        return CapabilityImportReview.model_validate(normalized)

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
