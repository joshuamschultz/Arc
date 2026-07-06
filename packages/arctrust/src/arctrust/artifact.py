"""Detached artifact signatures — content-hash + Ed25519 over arbitrary bytes.

SPEC-033 A1. arctrust owns the sign/verify primitives; higher layers (the
arcagent capability loader) call these to sign agent-authored artifacts on
write and re-verify them at load — independent of any install-time check.

Honest semantics: a valid signature proves the bytes are *unmodified since the
signer wrote them* and *attributes* them to the signer's DID key. It does NOT
prove the content is safe — a compromised signer produces a perfectly valid
signature over malicious bytes. Safety belongs to the TOFU gate and the
execution sandbox, never to this primitive.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from arctrust.keypair import KeyPair, sign, verify

_ALGORITHM = "ed25519"


def content_sha256(content: bytes) -> str:
    """Return the ``sha256:<hex>`` digest of ``content``."""
    return "sha256:" + hashlib.sha256(content).hexdigest()


class ArtifactSignature(BaseModel):
    """Detached signature manifest written beside a signed artifact.

    Serialised to a ``.arcsig`` sidecar. Carries everything a verifier needs
    to re-check the artifact bytes at load with no external lookup: the content
    digest, the signer DID, the signer's Ed25519 public key, and the signature.
    """

    model_config = ConfigDict(frozen=True)

    artifact_sha256: str
    signer_did: str
    public_key: str
    """Hex-encoded 32-byte Ed25519 verify key of the signer."""

    signature: str
    """Hex-encoded 64-byte Ed25519 signature over the raw artifact bytes."""

    algorithm: str = _ALGORITHM
    signed_at: str | None = None

    def to_json(self) -> str:
        """Serialise to the ``.arcsig`` sidecar payload."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> ArtifactSignature:
        """Parse a ``.arcsig`` sidecar payload."""
        return cls.model_validate_json(raw)


def sign_artifact(
    content: bytes, *, signer_did: str, private_key: bytes
) -> ArtifactSignature:
    """Sign ``content`` with an Ed25519 private-key seed under ``signer_did``."""
    public_key = KeyPair.from_seed(private_key).public_key
    signature = sign(content, private_key)
    return ArtifactSignature(
        artifact_sha256=content_sha256(content),
        signer_did=signer_did,
        public_key=public_key.hex(),
        signature=signature.hex(),
        signed_at=datetime.now(UTC).isoformat(),
    )


def verify_artifact(
    content: bytes,
    manifest: ArtifactSignature,
    *,
    trusted_public_key: bytes | None = None,
) -> bool:
    """Re-verify signed ``content`` against its manifest at load time.

    Returns True iff (a) the content digest matches, (b) the Ed25519 signature
    verifies against the manifest's embedded public key, and (c) — when a
    ``trusted_public_key`` is pinned — the manifest's key equals it. Never
    raises; any malformed field collapses to False (fail-closed).
    """
    if manifest.algorithm != _ALGORITHM:
        return False
    if manifest.artifact_sha256 != content_sha256(content):
        return False
    try:
        public_key = bytes.fromhex(manifest.public_key)
        signature = bytes.fromhex(manifest.signature)
    except ValueError:
        return False
    if trusted_public_key is not None and public_key != trusted_public_key:
        return False
    return verify(content, signature, public_key)


__all__ = [
    "ArtifactSignature",
    "content_sha256",
    "sign_artifact",
    "verify_artifact",
]
