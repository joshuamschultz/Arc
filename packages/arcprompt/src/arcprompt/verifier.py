"""SignatureVerifier over arctrust with mandatory key pinning (COMP-003).

Every overlay is verified before its text can enter a model context, at every
tier, with no bypass flag (REQ-129). Crypto is delegated to
``arctrust.artifact.verify_artifact`` — arcprompt never reimplements it.

Pinning is mandatory. An unpinned signature gate accepts *any* self-consistent
signature: an attacker self-signs a malicious overlay with a random keypair and
it verifies. So a ``None`` pinned key is refused (verification returns False),
not skipped — an unpinned floor is no floor (the blueprint-loader HIGH-1 lesson).
"""

from __future__ import annotations

from enum import StrEnum

from arctrust.artifact import ArtifactSignature, verify_artifact


class TrustPosture(StrEnum):
    """Deployment stringency carried for provenance/audit context.

    Verification is unconditional regardless of posture (REQ-129); posture is
    recorded in the run-provenance event (REQ-132) so a federal agent's audit
    trail never reads a personal/default value (the SPEC-017 audit-lies lesson).
    """

    PERSONAL = "personal"
    ENTERPRISE = "enterprise"
    FEDERAL = "federal"


class SignatureVerifier:
    """Verify an overlay's detached signature against a single pinned public key."""

    def __init__(self, trusted_public_key: bytes | None) -> None:
        self._pinned = trusted_public_key

    def verify(self, content: bytes, manifest: ArtifactSignature) -> bool:
        """Return True only when the pin exists AND the artifact verifies against it.

        A missing pin fails closed. Delegates digest + Ed25519 + key-equality to
        ``verify_artifact`` (which never raises; malformed fields collapse to False).
        """
        if self._pinned is None:
            return False
        return verify_artifact(content, manifest, trusted_public_key=self._pinned)


__all__ = [
    "SignatureVerifier",
    "TrustPosture",
]
