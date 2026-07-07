"""Pluggable trust backends for load-time verification (SPEC-033 F2/REQ-051).

The capability loader depends only on the :class:`TrustBackend` Protocol, so the
verification mechanism can be swapped without touching the load loop. Selection
is source/tier-aware (SDD C6):

- **Ed25519/DID** (arctrust) — self-authored artifacts and air-gapped/federal
  deployments: network-free, reuses the per-agent DID key ADR-019 mandates.
- **Sigstore keyless** (arcskill) — install-time hub skills, where real external
  OIDC/Fulcio provenance exists.

Only the Ed25519 backend is wired into the load path here; the Sigstore backend
governs install-time hub verification (arcskill owns it). Both satisfy one
``verify`` signature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from arcagent.capabilities import artifact_signing


@runtime_checkable
class TrustBackend(Protocol):
    """One-method verify contract the loader depends on."""

    def verify(
        self, artifact: Path, content: bytes, *, trusted_public_key: bytes | None = None
    ) -> bool:
        """Return True iff ``content`` is authentic for ``artifact``. Fail-closed."""
        ...


class Ed25519TrustBackend:
    """DID-scoped Ed25519 detached-signature verify (arctrust). Default backend."""

    def verify(
        self, artifact: Path, content: bytes, *, trusted_public_key: bytes | None = None
    ) -> bool:
        return artifact_signing.verify_file(
            artifact, content, trusted_public_key=trusted_public_key
        )


__all__ = ["Ed25519TrustBackend", "TrustBackend"]
