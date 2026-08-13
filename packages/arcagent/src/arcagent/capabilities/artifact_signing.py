"""Sidecar signing convention for agent-authored artifacts (SPEC-033).

The capability loader and the self-modification tools share one detached-
signature convention: a signed artifact ``X`` has a ``X.arcsig`` sidecar
carrying an :class:`arctrust.ArtifactSignature` over the artifact bytes. The
tools write it on create/update/mutation; the loader re-verifies it at load,
independent of any install-time check.

arctrust owns the crypto (content hash + an algorithm-dispatched signature).
This module owns only the on-disk convention — where the sidecar lives and how a
corrupt one is treated (as unsigned, fail-closed).

Two ways in, one convention out: :func:`write_signature` for a caller that holds
an Ed25519 seed (an agent signing with its own DID key), and
:func:`write_signature_with_signer` for one that signs through an
:class:`arctrust.Signer` — which is how an enterprise/federal operator signs
without the seed ever entering this process.
"""

from __future__ import annotations

import logging
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from arctrust import (
    ArtifactSignature,
    Signer,
    sign_artifact,
    sign_artifact_with_signer,
    verify_artifact,
)

SIDECAR_SUFFIX = ".arcsig"

_logger = logging.getLogger("arcagent.capabilities.artifact_signing")


def sidecar_path(artifact: Path) -> Path:
    """Return the ``.arcsig`` sidecar path beside ``artifact``."""
    return artifact.with_name(artifact.name + SIDECAR_SUFFIX)


def write_signature(
    artifact: Path, content: bytes, *, signer_did: str, private_key: bytes
) -> Path:
    """Sign ``content`` with an Ed25519 seed under ``signer_did``; write the sidecar."""
    return _write(artifact, sign_artifact(content, signer_did=signer_did, private_key=private_key))


def write_signature_with_signer(
    artifact: Path, content: bytes, *, signer_did: str, signer: Signer
) -> Path:
    """Sign ``content`` through ``signer`` under ``signer_did``; write the sidecar.

    The custody-agnostic entry point: the sidecar records the signer's own
    algorithm and verify key, so a vault-transit ECDSA-P256 signature lands in
    exactly the same convention an in-process Ed25519 one does.
    """
    return _write(
        artifact, sign_artifact_with_signer(content, signer_did=signer_did, signer=signer)
    )


def _write(artifact: Path, manifest: ArtifactSignature) -> Path:
    """Persist ``manifest`` as ``artifact``'s sidecar and return the sidecar path.

    A module's capability surface lives in the deployment module root, which
    :mod:`arcbundle.materializer` hardens to ``0444`` inside ``0555`` so an
    in-place edit fails loudly instead of silently forking a signed module.
    Re-signing is the operator action that legitimises such an edit, so it
    borrows write permission for the one write and hands it straight back —
    the same restore-then-modify sequence ``arcbundle.remove`` uses. Everywhere
    else (an agent signing in its own workspace) the modes already allow the
    write and nothing is touched.
    """
    target = sidecar_path(artifact)
    payload = manifest.to_json()
    with _writable(target.parent), _writable(target):
        target.write_text(payload, encoding="utf-8")
    return target


@contextmanager
def _writable(path: Path) -> Iterator[None]:
    """Grant the owner write on ``path`` for the block, restoring the mode after.

    A path that does not exist, or whose mode cannot be read or changed, is
    yielded to unchanged: the write that follows then fails on its own terms
    with the real errno rather than being masked by a permission fix-up.
    """
    try:
        original = path.stat().st_mode
    except OSError:
        yield
        return
    if original & stat.S_IWUSR:
        yield
        return
    try:
        path.chmod(original | stat.S_IWUSR)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        with suppress(OSError):
            path.chmod(original)


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
    "write_signature_with_signer",
]
