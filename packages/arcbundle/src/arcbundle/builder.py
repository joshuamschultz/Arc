"""Package one module source folder into a signed bundle.

Runs on the low side: it needs the source tree, a signing key, and nothing else
— no network, no agent stack, no package index. That is what lets an operator
stage a bundle on a build host and carry it to an air-gapped enclave on approved
media, and it is why release CI and ``--from-source`` can share one code path.

A bundle is a directory: ``manifest.json``, ``manifest.sig``, and a ``files/``
payload tree, conventionally named ``<module>.arcbundle``. That is the exact
shape :func:`arcbundle.verify_bundle` reads, so every bundle this builds
round-trips through the verifier rather than through a second reader.

Two signatures, one key. The manifest signature says *this bundle is what its
issuer built*; a per-file ``.arcsig`` beside each capability artifact says *this
file is what its issuer signed*, which is the question the capability loader
asks long after the bundle directory is gone.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath

from arctrust import ArtifactSignature, Signer, sign_artifact, sign_artifact_with_signer

from arcbundle.capability_copy import SIGNATURE_SIDECAR_SUFFIX
from arcbundle.errors import BundleMaterializeError
from arcbundle.manifest import MANIFEST_FORMAT_VERSION, BundleManifest, FileEntry
from arcbundle.signer import sign_manifest
from arcbundle.verifier import MANIFEST_NAME, PAYLOAD_DIR, SIGNATURE_NAME

#: The skill manifest the capability loader gates a skill folder on. Spelled
#: here as an on-disk convention, not imported: arcbundle is a leaf.
SKILL_MANIFEST = "SKILL.md"
PY_SUFFIX = ".py"

__all__ = ["build_bundle"]

_logger = logging.getLogger("arcbundle.builder")

# Build artifacts, not source. They differ between machines and Python versions,
# so including them would make the manifest's hashes unreproducible.
_EXCLUDED_NAMES = frozenset({"__pycache__", ".DS_Store"})
_EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


def build_bundle(
    source_dir: Path,
    *,
    module: str,
    version: str,
    private_key: bytes | Signer,
    issuer: str,
    out: Path,
) -> Path:
    """Build a signed bundle from ``source_dir`` at ``out`` and return its path.

    Args:
        source_dir: The module folder to package. Walked recursively; build
            artifacts are skipped and symlinks are never followed.
        module: Module name, which becomes the installed directory name.
        version: Version string recorded in the manifest.
        private_key: Raw 32-byte Ed25519 seed, or an ``arctrust.Signer``.
        issuer: Identifier signed into the manifest.
        out: Bundle directory to create. Must not already exist, so a build can
            never half-overwrite an earlier bundle.

    Returns:
        ``out``, now holding the manifest, its signature, and the payload tree.

    Raises:
        BundleMaterializeError: ``source_dir`` is unusable or ``out`` exists.
        BundleManifestError: A source path or the module name is not safe to
            declare, or ``issuer`` disagrees with what is being signed.
    """
    if not source_dir.is_dir():
        raise BundleMaterializeError(f"not a module source directory: {source_dir}")
    if out.exists() or out.is_symlink():
        raise BundleMaterializeError(f"bundle output already exists: {out}")

    payload_root = out / PAYLOAD_DIR
    payload: dict[str, bytes] = {}
    for source in _payload_files(source_dir):
        relative = source.relative_to(source_dir).as_posix()
        payload[relative] = source.read_bytes()

    if not payload:
        shutil.rmtree(out, ignore_errors=True)
        raise BundleMaterializeError(f"module source {source_dir} contains no files to bundle")

    payload.update(_capability_sidecars(payload, private_key=private_key, issuer=issuer))

    entries: list[FileEntry] = []
    for relative, data in payload.items():
        target = payload_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # The digest is taken from the bytes just copied, so the manifest
        # describes the payload as written rather than as read a moment earlier.
        entries.append(FileEntry(path=relative, sha256=hashlib.sha256(data).hexdigest()))

    manifest = BundleManifest(
        format_version=MANIFEST_FORMAT_VERSION,
        module=module,
        version=version,
        issuer=issuer,
        files=sorted(entries, key=lambda entry: entry.path),
    )
    (out / MANIFEST_NAME).write_bytes(manifest.canonical_bytes())
    (out / SIGNATURE_NAME).write_bytes(
        sign_manifest(manifest, private_key=private_key, issuer=issuer)
    )

    _logger.info(
        "bundle built: module=%s version=%s issuer=%s files=%d path=%s",
        module,
        version,
        issuer,
        len(entries),
        out,
    )
    return out


def _capability_sidecars(
    payload: Mapping[str, bytes], *, private_key: bytes | Signer, issuer: str
) -> dict[str, bytes]:
    """Sign each adjudicated artifact in ``payload``; return its ``.arcsig`` bytes.

    The capability loader adjudicates a module by detached signature — the same
    per-file sidecar convention agent-authored tools and skills already use, so
    an operator has one model: a capability is signed, and modifying it means
    re-signing it. Signing them here is what makes that true out of the box,
    with the same key that signs the manifest.

    Included in the manifest like any other payload file, and therefore covered
    by the manifest signature and by the verifier's refusal of undeclared files:
    a sidecar swapped in transit is a content-hash mismatch, not a new trust
    anchor. Any sidecar already sitting in the source tree is superseded rather
    than trusted.
    """
    sidecars: dict[str, bytes] = {}
    for relative, data in payload.items():
        if not _is_adjudicated_artifact(relative):
            continue
        signature = _sign_artifact(data, private_key=private_key, issuer=issuer)
        sidecars[relative + SIGNATURE_SIDECAR_SUFFIX] = signature.to_json().encode("utf-8")
    return sidecars


def _is_adjudicated_artifact(relative: str) -> bool:
    """True for the files the capability loader executes or injects.

    Every ``.py``, not only ``capabilities.py``: the loader imports each ``.py``
    at a module root looking for decorated values, and an import runs the file.
    Signing the declared capability surface while leaving ``config.py`` beside it
    unsigned would verify the door and not the wall. Plus every ``SKILL.md``,
    whose text is injected into the agent's prompt (LLM01/ASI06).
    """
    name = PurePosixPath(relative).name
    return name.endswith(PY_SUFFIX) or name == SKILL_MANIFEST


def _sign_artifact(
    content: bytes, *, private_key: bytes | Signer, issuer: str
) -> ArtifactSignature:
    """Sign one artifact's bytes under ``issuer``, whatever the key custody is."""
    if isinstance(private_key, bytes):
        return sign_artifact(content, signer_did=issuer, private_key=private_key)
    return sign_artifact_with_signer(content, signer_did=issuer, signer=private_key)


def _payload_files(source_dir: Path) -> Iterator[Path]:
    """Yield every packageable file under ``source_dir``, in a stable order.

    Symlinks are skipped rather than followed: a link in a source checkout can
    point anywhere, and a bundle must contain only what it declares.
    """
    for path in sorted(source_dir.rglob("*")):
        if path.is_symlink():
            continue
        if any(part in _EXCLUDED_NAMES for part in path.relative_to(source_dir).parts):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES:
            continue
        if path.is_file():
            yield path
