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
    copy_capabilities   — per-agent copy of a module's tools + skills
    remove_capabilities — the inverse; False when there was nothing to remove
    capability_dir      — the one destination path rule both halves share
    CAPABILITIES_DIR / CAPABILITY_FILE / SKILLS_DIR / SIGNATURE_SIDECAR_SUFFIX
                        — the on-disk names the copy is defined in terms of

    BundleError         — base of every refusal below
    BundleManifestError / BundleSignatureError / BundleContentHashError /
    BundleMaterializeError — one per failed gate; all mean nothing was installed

Audit events (``module.bundle.verified``, ``module.signature_invalid``,
``module.content_hash_mismatch``, ``module.installed``, ``module.removed``) are
emitted from inside this package, at the point each outcome is decided, so every
surface that installs a bundle records the same fact. Pass ``sink=`` (and
``actor_did=`` where an operator is known) to ``verify_bundle``, ``materialize``,
and ``remove``; sinks fan out from ``arctrust.audit.emit`` unchanged.
"""

from __future__ import annotations

from arcbundle.builder import build_bundle
from arcbundle.capability_copy import (
    CAPABILITIES_DIR,
    CAPABILITY_FILE,
    SIGNATURE_SIDECAR_SUFFIX,
    SKILLS_DIR,
    capability_dir,
    copy_capabilities,
    remove_capabilities,
)
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
    "CAPABILITIES_DIR",
    "CAPABILITY_FILE",
    "DEV_ISSUER",
    "DEV_ISSUER_TIERS",
    "DIR_MODE",
    "FILE_MODE",
    "MANIFEST_FORMAT_VERSION",
    "MANIFEST_NAME",
    "PAYLOAD_DIR",
    "SIGNATURE_NAME",
    "SIGNATURE_SIDECAR_SUFFIX",
    "SKILLS_DIR",
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
    "capability_dir",
    "copy_capabilities",
    "materialize",
    "remove",
    "remove_capabilities",
    "sign_manifest",
    "verify_bundle",
]
