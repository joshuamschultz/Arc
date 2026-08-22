"""Typed models for the capability-import intake boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class CapabilityImportReviewFile(BaseModel):
    """Bounded file metadata safe to expose from a staged import."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=200 * 1024 * 1024)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        parts = value.split("/")
        if "\\" in value or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("review file path is unsafe")
        if parts[0] not in {"tools", "skills"}:
            raise ValueError("review file path is outside capability roots")
        return value


class CapabilityImportReview(BaseModel):
    """The strict, metadata-only wire contract for staged import reviews."""

    model_config = ConfigDict(extra="forbid", strict=True)

    _SHA256: ClassVar[str] = r"^[0-9a-f]{64}$"

    import_id: str = Field(min_length=64, max_length=64, pattern=_SHA256)
    status: CapabilityImportStatus
    target_agent_did: str = Field(min_length=1, max_length=256)
    archive_sha256: str = Field(pattern=_SHA256)
    review_digest: str = Field(pattern=_SHA256)
    files: list[CapabilityImportReviewFile] = Field(max_length=512)
    tools: list[str] = Field(max_length=512)
    skills: list[str] = Field(max_length=512)
    supplier_sbom_sha256: str | None = Field(default=None, pattern=_SHA256)
    supplier_metadata_keys: list[str] = Field(max_length=128)
    activation: Literal["review_only"]

    @field_validator("tools", "skills", "supplier_metadata_keys")
    @classmethod
    def validate_names(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("review metadata name is out of bounds")
        return values
