"""The on-disk bundle shape, and the one byte form its signature binds.

A signature is only meaningful if the signer and every verifier serialize the
same object to the same bytes, so ``canonical_bytes`` delegates to
``arctrust.canonical_json`` — the single primitive `arcrun` already signs its
backend manifests with. A second encoder here, however faithful today, is the
drift that later shows up in the field as an unexplained signature rejection.
"""

from __future__ import annotations

import re
from typing import Self

from arctrust import canonical_json
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from arcbundle._paths import require_relative_path, require_safe_name
from arcbundle.errors import BundleManifestError

__all__ = ["MANIFEST_FORMAT_VERSION", "BundleManifest", "FileEntry"]

# Bumped only when the signed shape changes; a verifier refuses what it cannot
# interpret rather than guessing at an unknown layout.
MANIFEST_FORMAT_VERSION = 1

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class FileEntry(BaseModel):
    """One declared payload file: where it lands, and what it must hash to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str

    @field_validator("path")
    @classmethod
    def _path_stays_inside_the_bundle(cls, value: str) -> str:
        return require_relative_path(value, field="file entry path")

    @field_validator("sha256")
    @classmethod
    def _digest_is_lowercase_hex(cls, value: str) -> str:
        """Pin one spelling of a digest so equality is a byte comparison.

        Two spellings of the same hash mean two manifests that are equal in
        meaning but not in bytes, which the canonical encoding cannot reconcile.
        """
        if not _SHA256_HEX.match(value):
            raise BundleManifestError(f"sha256 must be 64 lowercase hex characters, got {value!r}")
        return value


class BundleManifest(BaseModel):
    """The complete description of a module bundle, and the object that is signed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: int
    module: str
    version: str
    issuer: str
    files: list[FileEntry]

    @field_validator("format_version")
    @classmethod
    def _format_version_is_known(cls, value: int) -> int:
        if value != MANIFEST_FORMAT_VERSION:
            raise BundleManifestError(
                f"unsupported manifest format_version {value!r}; "
                f"this build reads {MANIFEST_FORMAT_VERSION}"
            )
        return value

    @field_validator("module")
    @classmethod
    def _module_is_one_directory_component(cls, value: str) -> str:
        """The module name is the directory materialize creates, so it is a path too."""
        return require_safe_name(value, field="module")

    @field_validator("version", "issuer")
    @classmethod
    def _identifier_is_present(cls, value: str) -> str:
        if not value.strip():
            raise BundleManifestError("version and issuer must not be blank")
        return value

    @model_validator(mode="after")
    def _paths_are_declared_once(self) -> Self:
        """Two entries for one path would let the second silently shadow the first."""
        declared = [entry.path for entry in self.files]
        if len(set(declared)) != len(declared):
            raise BundleManifestError("manifest declares the same path more than once")
        return self

    def canonical_bytes(self) -> bytes:
        """Return the deterministic bytes a signature over this manifest binds."""
        return canonical_json(self.model_dump(mode="json"))
