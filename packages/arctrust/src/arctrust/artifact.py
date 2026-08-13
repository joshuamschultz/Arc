"""Detached artifact signatures — content-hash + asymmetric signature over bytes.

SPEC-033 A1. arctrust owns the sign/verify primitives; higher layers (the
arcagent capability loader) call these to sign agent-authored artifacts on
write and re-verify them at load — independent of any install-time check.

Signing goes through :class:`~arctrust.signer.Signer`, so key custody is the
caller's config decision and not this module's business: a personal-tier seed
signs in this process, while an enterprise/federal vault or notary key signs by
reference and never enters it. Verification dispatches on the manifest's
recorded ``algorithm`` through :func:`~arctrust.signer.verify_signature`, so
Ed25519 and ECDSA-P256 artifacts verify through one path and an unsupported
algorithm fails closed.

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

from arctrust.signer import ED25519, InProcessSigner, Signer, verify_signature


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
    """Hex-encoded verify key of the signer, in ``algorithm``'s own encoding."""

    signature: str
    """Hex-encoded signature over the raw artifact bytes, made by ``algorithm``."""

    algorithm: str = ED25519
    """Which primitive signed these bytes — it selects the verifier, so it is
    part of what a pinned key protects. See :func:`verify_artifact`."""

    signed_at: str | None = None

    def to_json(self) -> str:
        """Serialise to the ``.arcsig`` sidecar payload."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> ArtifactSignature:
        """Parse a ``.arcsig`` sidecar payload."""
        return cls.model_validate_json(raw)


def sign_artifact_with_signer(
    content: bytes, *, signer_did: str, signer: Signer
) -> ArtifactSignature:
    """Sign ``content`` under ``signer_did`` through a :class:`Signer`.

    The custody-agnostic path: an in-process seed and a vault/notary key produce
    the same manifest, because the signer is what holds the private material.
    The recorded algorithm and public key are the signer's own, so a federal
    ECDSA-P256 artifact carries the ECDSA verify key rather than an Ed25519 one.
    """
    return ArtifactSignature(
        artifact_sha256=content_sha256(content),
        signer_did=signer_did,
        public_key=signer.public_key.hex(),
        signature=signer.sign(content).hex(),
        algorithm=signer.algorithm,
        signed_at=datetime.now(UTC).isoformat(),
    )


def sign_artifact(content: bytes, *, signer_did: str, private_key: bytes) -> ArtifactSignature:
    """Sign ``content`` with an Ed25519 private-key seed under ``signer_did``.

    The in-process custody path (personal tier, and any caller that legitimately
    holds the seed — an agent signing with its own DID key).

    Raises:
        ValueError: ``private_key`` is not a valid 32-byte Ed25519 seed.
    """
    return sign_artifact_with_signer(
        content, signer_did=signer_did, signer=InProcessSigner(private_key, ED25519)
    )


def verify_artifact(
    content: bytes,
    manifest: ArtifactSignature,
    *,
    trusted_public_key: bytes | None = None,
) -> bool:
    """Re-verify signed ``content`` against its manifest at load time.

    Returns True iff (a) the content digest matches, (b) the signature verifies
    under the manifest's OWN recorded ``algorithm`` against its embedded public
    key, and (c) — when a ``trusted_public_key`` is pinned — the manifest's key
    equals it. Never raises; any malformed field collapses to False.

    The algorithm is dispatched, never assumed: :func:`verify_signature` returns
    False for any value outside the supported set, so relabelling a manifest can
    only ever refuse it. An empty or unknown algorithm therefore fails closed
    rather than falling back to the Ed25519 default — an attacker must not be
    able to choose which verifier (or none) runs.
    """
    if manifest.artifact_sha256 != content_sha256(content):
        return False
    try:
        public_key = bytes.fromhex(manifest.public_key)
        signature = bytes.fromhex(manifest.signature)
    except ValueError:
        return False
    if trusted_public_key is not None and public_key != trusted_public_key:
        return False
    return verify_signature(manifest.algorithm, content, signature, public_key)


__all__ = [
    "ArtifactSignature",
    "content_sha256",
    "sign_artifact",
    "sign_artifact_with_signer",
    "verify_artifact",
]
