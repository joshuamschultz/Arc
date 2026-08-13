"""Detached Ed25519 signature over a manifest's canonical bytes.

Detached on purpose: the signature never becomes part of what is signed, so
there is no envelope to strip and no self-referential field to reason about.
The verifier reads ``manifest.json`` and ``manifest.sig`` as two files and can
re-derive the signed bytes from the model alone.

Used by release CI with the Arc release key and by ``--from-source`` with a
locally generated development key. Both go through this one function, so the
development path is the release path with a different key — never a shortcut.
"""

from __future__ import annotations

from arctrust import ED25519, Signer, sign

from arcbundle.errors import BundleManifestError, BundleSignatureError
from arcbundle.manifest import BundleManifest

__all__ = ["sign_manifest"]


def sign_manifest(
    manifest: BundleManifest,
    *,
    private_key: bytes | Signer,
    issuer: str,
) -> bytes:
    """Return the 64-byte detached signature over ``manifest.canonical_bytes()``.

    Args:
        manifest: The manifest to sign. Its own ``issuer`` field is part of the
            signed bytes, which is what makes the issuer stamped rather than
            merely asserted alongside.
        private_key: A raw 32-byte Ed25519 seed, or an ``arctrust.Signer`` for
            custody models where the key never enters this process.
        issuer: The identifier being signed for. Must match ``manifest.issuer``,
            otherwise the caller believes it signed something it did not.

    Returns:
        The detached signature bytes. Never writes payload files, and never
        writes the signature — the caller decides where it lands.

    Raises:
        BundleManifestError: ``issuer`` disagrees with ``manifest.issuer``.
        BundleSignatureError: The supplied signer does not produce Ed25519.
    """
    if issuer != manifest.issuer:
        raise BundleManifestError(
            f"issuer {issuer!r} does not match the manifest's issuer {manifest.issuer!r}"
        )

    payload = manifest.canonical_bytes()
    if isinstance(private_key, bytes):
        return sign(payload, private_key)

    # A Signer may be backed by Vault or an HSM and may be configured for a
    # different algorithm; a bundle signature is Ed25519 by definition, so an
    # algorithm mismatch is refused here rather than discovered at verify time.
    if private_key.algorithm != ED25519:
        raise BundleSignatureError(
            f"bundle signatures are {ED25519}; signer offers {private_key.algorithm!r}"
        )
    return private_key.sign(payload)
