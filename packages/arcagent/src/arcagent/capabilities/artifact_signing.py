"""Sidecar signing convention for agent-authored artifacts (SPEC-033).

The capability loader and the self-modification tools share one detached-
signature convention: a signed artifact ``X`` has a ``X.arcsig`` sidecar
carrying an :class:`arctrust.ArtifactSignature` over the artifact bytes. The
tools write it on create/update/mutation; the loader re-verifies it at load,
independent of any install-time check.

arctrust owns the crypto (content hash + Ed25519). This module owns only the
on-disk convention — where the sidecar lives and how a corrupt one is treated
(as unsigned, fail-closed).
"""

from __future__ import annotations

import logging
from pathlib import Path

from arctrust import ArtifactSignature, sign_artifact, verify_artifact

SIDECAR_SUFFIX = ".arcsig"

_logger = logging.getLogger("arcagent.capabilities.artifact_signing")


def sidecar_path(artifact: Path) -> Path:
    """Return the ``.arcsig`` sidecar path beside ``artifact``."""
    return artifact.with_name(artifact.name + SIDECAR_SUFFIX)


def write_signature(
    artifact: Path, content: bytes, *, signer_did: str, private_key: bytes
) -> Path:
    """Sign ``content`` under ``signer_did`` and write the sidecar. Returns it."""
    manifest = sign_artifact(content, signer_did=signer_did, private_key=private_key)
    target = sidecar_path(artifact)
    target.write_text(manifest.to_json(), encoding="utf-8")
    return target


def load_signature(artifact: Path) -> ArtifactSignature | None:
    """Load the sidecar manifest for ``artifact``, or ``None`` if absent/corrupt."""
    sidecar = sidecar_path(artifact)
    if not sidecar.exists():
        return None
    try:
        return ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    except Exception:  # reason: a corrupt/forged sidecar is treated as unsigned (fail-closed)
        _logger.warning("unreadable signature sidecar at %s; treating as unsigned", sidecar)
        return None


def verify_file(
    artifact: Path, content: bytes, *, trusted_public_key: bytes | None = None
) -> bool:
    """Re-verify ``content`` against ``artifact``'s sidecar at load. Fail-closed."""
    manifest = load_signature(artifact)
    if manifest is None:
        return False
    return verify_artifact(content, manifest, trusted_public_key=trusted_public_key)


__all__ = [
    "SIDECAR_SUFFIX",
    "load_signature",
    "sidecar_path",
    "verify_file",
    "write_signature",
]
