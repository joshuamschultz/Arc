"""arcbundle — signed module bundles for Arc (SPEC-066).

The distribution unit that makes a module's ABSENCE provable. The `arc-agent`
wheel carries no module code and is byte-identical across every tier; each
module ships as a separately signed bundle, verified in full before a single
byte reaches disk, and materialized read-only at the deployment root outside
every agent's tool fence.

Layer: a leaf beside `arctrust`. It imports `arctrust` and Pydantic and nothing
else — never `arcagent`. The agent only ever READS an already-materialized
directory, so the nucleus stays ignorant of distribution, and a bundle can be
built or verified on a low-side box with no agent stack present.

Public surface:

    BundleManifest      — the on-disk bundle shape + canonical-JSON encoding
    FileEntry           — one declared payload file: relative path + sha256
    MANIFEST_FORMAT_VERSION — the signed shape this build reads
    sign_manifest       — detached Ed25519 signature over canonical bytes
    verify_bundle       — fail-closed verify; no filesystem mutation on any path
    VerifiedBundle      — the only thing materialize accepts; carries verified bytes
    DEV_ISSUER          — the development issuer, trusted at personal tier only
    DEV_ISSUER_TIERS    — the tiers that accept it
    TIERS               — the deployment tiers a bundle may be verified for
    MANIFEST_NAME / SIGNATURE_NAME / PAYLOAD_DIR — the on-disk bundle layout
    materialize         — atomic write, 0444 files inside 0555 directories
    FILE_MODE / DIR_MODE — the modes materialize applies
    remove              — the inverse; raises if it does not complete
    build_bundle        — package a module folder into a signed .arcbundle

    BundleError         — base of every refusal below
    BundleManifestError / BundleSignatureError / BundleContentHashError /
    BundleMaterializeError — one per failed gate; all mean nothing was installed
"""

from __future__ import annotations

from arcbundle.builder import build_bundle
from arcbundle.errors import (
    BundleContentHashError,
    BundleError,
    BundleManifestError,
    BundleMaterializeError,
    BundleSignatureError,
)
from arcbundle.manifest import MANIFEST_FORMAT_VERSION, BundleManifest, FileEntry
from arcbundle.materializer import DIR_MODE, FILE_MODE, materialize, remove
from arcbundle.signer import sign_manifest
from arcbundle.verifier import (
    DEV_ISSUER,
    DEV_ISSUER_TIERS,
    MANIFEST_NAME,
    PAYLOAD_DIR,
    SIGNATURE_NAME,
    TIERS,
    VerifiedBundle,
    verify_bundle,
)

__version__ = "0.9.0"

__all__ = [
    "DEV_ISSUER",
    "DEV_ISSUER_TIERS",
    "DIR_MODE",
    "FILE_MODE",
    "MANIFEST_FORMAT_VERSION",
    "MANIFEST_NAME",
    "PAYLOAD_DIR",
    "SIGNATURE_NAME",
    "TIERS",
    "BundleContentHashError",
    "BundleError",
    "BundleManifest",
    "BundleManifestError",
    "BundleMaterializeError",
    "BundleSignatureError",
    "FileEntry",
    "VerifiedBundle",
    "build_bundle",
    "materialize",
    "remove",
    "sign_manifest",
    "verify_bundle",
]
