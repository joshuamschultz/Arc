"""Fail-closed bundle verification — everything is checked before anything is written.

The order is the whole point: signature against the tier's trusted issuer set,
then the canonical form of the manifest, then every declared file hash, then a
sweep for files the manifest never declared. Only after all of that does a
``VerifiedBundle`` exist, and only a ``VerifiedBundle`` can be materialized.

Two properties this module owes its callers:

* **No filesystem mutation on any path.** Verification reads; it never creates,
  writes, or removes. A refusal therefore cannot leave a partial tree, because
  nothing had been written to leave behind.
* **Fail closed.** There is exactly one ``return``, on the last line, reached
  only when every gate above it has passed. Each gate catches precisely what it
  can provoke and converts it into a refusal, so an unexpected exception
  propagates as a denial rather than being mistaken for a pass.

The verified payload is carried *in memory* rather than re-read at materialize
time. That closes the window between "these bytes hashed correctly" and "these
bytes were written": what lands on disk is the exact content that was verified,
not whatever occupies the path a moment later.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from arctrust import verify
from pydantic import ValidationError

from arcbundle.errors import (
    BundleContentHashError,
    BundleManifestError,
    BundleSignatureError,
)
from arcbundle.manifest import BundleManifest

__all__ = [
    "DEV_ISSUER",
    "DEV_ISSUER_TIERS",
    "MANIFEST_NAME",
    "PAYLOAD_DIR",
    "SIGNATURE_NAME",
    "TIERS",
    "VerifiedBundle",
    "verify_bundle",
]

_logger = logging.getLogger("arcbundle.verifier")

MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.sig"
PAYLOAD_DIR = "files"

# The key `--from-source` signs with. It is a development convenience, never a
# release path, so the tiers that accept it are pinned here rather than in a CLI
# flag: no caller, present or future, can widen the set from the outside.
DEV_ISSUER = "did:arc:dev"
DEV_ISSUER_TIERS = frozenset({"personal"})

TIERS = frozenset({"personal", "enterprise", "federal"})


@dataclass(frozen=True)
class VerifiedBundle:
    """A bundle that passed every gate, plus the exact bytes that passed them."""

    root: Path
    manifest: BundleManifest
    files: Mapping[str, bytes]


def verify_bundle(
    bundle_root: Path,
    *,
    tier: str,
    trusted_issuers: Mapping[str, bytes],
) -> VerifiedBundle:
    """Verify a bundle in full and return it, or refuse and write nothing.

    Args:
        bundle_root: Directory holding ``manifest.json``, ``manifest.sig``, and
            the ``files/`` payload tree.
        tier: Deployment stringency — one of ``TIERS``. An unknown tier denies.
        trusted_issuers: Issuer identifier to 32-byte Ed25519 public key. An
            issuer absent from this mapping is untrusted by definition.

    Returns:
        The verified bundle, carrying the payload bytes that were hashed.

    Raises:
        BundleManifestError: Unreadable, malformed, or non-canonical manifest.
        BundleSignatureError: Untrusted issuer, wrong tier for the issuer, or a
            signature that does not verify.
        BundleContentHashError: A declared file is missing or altered, or the
            payload tree carries a file the manifest never declared.
    """
    raw_manifest, signature = _read_bundle_files(bundle_root)
    manifest = _parse_manifest(raw_manifest)
    public_key = _resolve_issuer_key(manifest.issuer, tier=tier, trusted_issuers=trusted_issuers)

    if not verify(raw_manifest, signature, public_key):
        raise BundleSignatureError(
            f"manifest signature does not verify for issuer {manifest.issuer!r}"
        )

    # The signature covers the bytes on disk; this pins those bytes to the one
    # canonical spelling, so a re-encoded manifest cannot mean one thing to this
    # parser and another to the next reader of the same signed blob.
    if raw_manifest != manifest.canonical_bytes():
        raise BundleManifestError(f"{MANIFEST_NAME} is not in canonical form")

    payload_root = bundle_root / PAYLOAD_DIR
    files = _read_declared_files(payload_root, manifest)
    _refuse_undeclared_files(payload_root, declared=set(files))

    _logger.info(
        "bundle verified: module=%s version=%s issuer=%s tier=%s files=%d",
        manifest.module,
        manifest.version,
        manifest.issuer,
        tier,
        len(files),
    )
    return VerifiedBundle(root=bundle_root, manifest=manifest, files=files)


def _read_bundle_files(bundle_root: Path) -> tuple[bytes, bytes]:
    """Read the manifest and its detached signature, or refuse."""
    try:
        raw_manifest = (bundle_root / MANIFEST_NAME).read_bytes()
    except OSError as exc:
        raise BundleManifestError(f"cannot read {bundle_root / MANIFEST_NAME}: {exc}") from exc

    try:
        signature = (bundle_root / SIGNATURE_NAME).read_bytes()
    except OSError as exc:
        raise BundleSignatureError(f"cannot read {bundle_root / SIGNATURE_NAME}: {exc}") from exc

    return raw_manifest, signature


def _parse_manifest(raw_manifest: bytes) -> BundleManifest:
    """Parse manifest bytes into the model, converting a schema failure to a refusal.

    ``BundleManifestError`` raised by the model's own path and name guards is
    not caught here — it is already the refusal this function would produce.
    """
    try:
        return BundleManifest.model_validate_json(raw_manifest)
    except ValidationError as exc:
        raise BundleManifestError(
            f"{MANIFEST_NAME} does not match the bundle schema: {exc}"
        ) from exc


def _resolve_issuer_key(
    issuer: str,
    *,
    tier: str,
    trusted_issuers: Mapping[str, bytes],
) -> bytes:
    """Return the public key this tier trusts for ``issuer``, or refuse."""
    if tier not in TIERS:
        raise BundleSignatureError(f"unknown deployment tier {tier!r}; expected one of {TIERS}")

    if issuer == DEV_ISSUER and tier not in DEV_ISSUER_TIERS:
        raise BundleSignatureError(
            f"the development issuer {DEV_ISSUER!r} is trusted at "
            f"{sorted(DEV_ISSUER_TIERS)} tier only, not at {tier!r}"
        )

    public_key = trusted_issuers.get(issuer)
    if public_key is None:
        raise BundleSignatureError(f"issuer {issuer!r} is not trusted at {tier!r} tier")
    return public_key


def _read_declared_files(payload_root: Path, manifest: BundleManifest) -> dict[str, bytes]:
    """Read and hash every declared file, refusing on the first mismatch."""
    resolved_root = _resolve(payload_root, description="payload root")
    files: dict[str, bytes] = {}

    for entry in manifest.files:
        source = _resolve(payload_root / entry.path, description=f"payload file {entry.path!r}")
        # The manifest path was validated as relative, but a symlinked directory
        # *inside* the payload tree could still land the read outside it.
        if not source.is_relative_to(resolved_root):
            raise BundleContentHashError(f"payload file {entry.path!r} escapes the payload root")
        if not source.is_file():
            raise BundleContentHashError(f"declared payload file {entry.path!r} is not a file")

        try:
            data = source.read_bytes()
        except OSError as exc:
            raise BundleContentHashError(
                f"cannot read payload file {entry.path!r}: {exc}"
            ) from exc

        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, entry.sha256):
            raise BundleContentHashError(
                f"payload file {entry.path!r} hashes to {digest}, manifest declares {entry.sha256}"
            )
        files[entry.path] = data

    return files


def _refuse_undeclared_files(payload_root: Path, *, declared: set[str]) -> None:
    """Refuse a payload tree carrying anything the manifest did not declare.

    An undeclared file is unverified code riding along with verified code — and
    a module runtime loaded by path does not care which of the two it imports.
    A symlink counts as undeclared whatever it points at, so a planted link
    cannot smuggle content in under a directory entry.
    """
    try:
        found = sorted(payload_root.rglob("*"))
    except OSError as exc:
        raise BundleContentHashError(f"cannot walk {payload_root}: {exc}") from exc

    for path in found:
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(payload_root).as_posix()
        if relative not in declared:
            raise BundleContentHashError(f"payload carries undeclared file {relative!r}")


def _resolve(path: Path, *, description: str) -> Path:
    """Resolve ``path`` with symlinks followed, converting an I/O failure to a refusal."""
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise BundleContentHashError(f"cannot resolve {description}: {exc}") from exc
