"""Quarantine scanner seam for gateway attachments.

The gateway never executes or parses uploaded content while taking custody.
Scanning is an injected policy seam so deployments can select an antivirus or
content-disarm implementation without making the core depend on one.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class ScanStatus(StrEnum):
    """Terminal or in-flight scanner verdicts."""

    QUARANTINED = "quarantined"
    CLEAN = "clean"
    REJECTED = "rejected"
    SCANNER_UNAVAILABLE = "scanner_unavailable"
    EXPIRED = "expired"
    DELETED = "deleted"


@runtime_checkable
class AttachmentScanner(Protocol):
    """Validate one quarantined file without executing its contents."""

    async def scan(self, path: Path, *, mime: str, sha256: str) -> ScanStatus:
        """Return a fail-closed scan verdict for ``path``."""
        ...


class CleanScanner:
    """Small default scanner for personal/test deployments.

    This scanner only represents the already-completed signature validation in
    :class:`MediaStore`; it does not claim to replace antivirus or CDR.
    Federal callers must inject a real scanner and reject this implementation.
    """

    async def scan(self, path: Path, *, mime: str, sha256: str) -> ScanStatus:
        del mime, sha256
        return ScanStatus.CLEAN if path.is_file() else ScanStatus.REJECTED


__all__ = ["AttachmentScanner", "CleanScanner", "ScanStatus"]
