"""ArtifactVerifier — no model loads unverified (SPEC-077 COMP-017, REQ-022).

Every model artifact behind the engine seam (STT, TTS, wake, any future
PersonaPlex) must be version-pinned, content-addressed and signed before use.
MIT/open licensing does not exempt it (LLM03/ASI04 supply chain). An artifact
missing any of the three is refused at load, not warned about.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


def digest_file(path: str | Path, *, chunk: int = 1 << 20) -> str:
    """SHA-256 of a model file — the content-address / provenance anchor."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            hasher.update(block)
    return hasher.hexdigest()


class UnverifiedArtifactError(RuntimeError):
    """A model artifact failed pin/digest/signature verification."""


@dataclass(frozen=True)
class ModelArtifact:
    """A model to load, with the provenance the verifier requires.

    Attributes:
        name: Human name for messages/audit.
        version: Pinned version — ``None`` means unpinned (refused).
        sha256: Content digest — ``None`` means no provenance (refused).
        signature_ok: Whether the signature verified against a trusted key.
    """

    name: str
    version: str | None
    sha256: str | None
    signature_ok: bool


class ArtifactVerifier:
    """Fail-closed verification of a model artifact before load."""

    def verify(self, artifact: ModelArtifact) -> None:
        if not artifact.version:
            raise UnverifiedArtifactError(f"{artifact.name}: unpinned version — refusing to load")
        if not artifact.sha256:
            raise UnverifiedArtifactError(f"{artifact.name}: no content digest — refusing to load")
        if not artifact.signature_ok:
            raise UnverifiedArtifactError(f"{artifact.name}: signature did not verify")


__all__ = ["ArtifactVerifier", "ModelArtifact", "UnverifiedArtifactError", "digest_file"]
