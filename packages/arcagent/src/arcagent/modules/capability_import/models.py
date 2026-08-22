"""Typed models for the capability-import intake boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CapabilityImportLimits:
    """Hard resource limits enforced before and during extraction."""

    max_compressed_bytes: int = 50 * 1024 * 1024
    max_expanded_bytes: int = 200 * 1024 * 1024
    max_file_bytes: int = 32 * 1024 * 1024
    max_files: int = 512
    max_depth: int = 16
    max_compression_ratio: int = 100

    def __post_init__(self) -> None:
        if any(value < 1 for value in self.__dict__.values()):
            raise ValueError("capability import limits must be positive")


@dataclass(frozen=True)
class CapabilityImportFile:
    """One normalized regular file copied into an intake staging tree."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class CapabilityImportResult:
    """The immutable staging result of a successful non-executing intake."""

    import_id: str
    archive_sha256: str
    quarantine_path: Path
    staging_dir: Path
    files: tuple[CapabilityImportFile, ...]


class CapabilityImportStatus(StrEnum):
    """Non-activating lifecycle states for an imported capability tree."""

    QUARANTINED = "quarantined"
    VALIDATED = "validated"
    REVIEW_READY = "review_ready"
    REJECTED = "rejected"
    MODIFIED = "modified"


@dataclass(frozen=True)
class CapabilityImportManifest:
    """Canonical review artifact binding a staged tree to one agent DID."""

    import_id: str
    target_agent_did: str
    archive_sha256: str
    files: tuple[CapabilityImportFile, ...]
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    findings: tuple[str, ...]
    limits: CapabilityImportLimits
    supplier_metadata: dict[str, Any]
    supplier_sbom_sha256: str | None
    review_digest: str
