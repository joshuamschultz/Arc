"""T-959 (RED), ASI05/ASI06 — a bundle cannot name a file outside its own tree.

A manifest entry is a filesystem path that a privileged, operator-run install
will write. If `../` or an absolute path survives into materialize, a bundle
stops being a module and becomes an arbitrary-write primitive against the box —
into `~/.arc`, into an agent workspace, into anything the installing user owns.

The hostile manifests below are crafted the way an attacker would: the payload
file genuinely exists at the escaping path and its digest genuinely matches, so
a verifier that only checks signatures and hashes accepts them. The only thing
that can refuse these is an explicit relative-path check on the manifest.
"""

from __future__ import annotations

import hashlib
import os.path
from collections.abc import Callable
from pathlib import Path

import pytest
from arctrust import canonical_json, generate_keypair, sign, verify

from arcbundle import BundleManifest, BundleManifestError, FileEntry, sign_manifest, verify_bundle

Snapshot = Callable[[Path], dict[str, bytes | None]]

_HOSTILE_CONTENT = b"# written outside the bundle root\n"


def test_the_signature_is_a_detached_ed25519_signature_over_the_canonical_bytes() -> None:
    """The premise the hostile manifests below are built on, asserted directly.

    If the signer ever wraps its output in an envelope instead of emitting the
    raw 64-byte signature, the crafted manifests stop being validly signed and
    the traversal tests would start passing for the wrong reason.
    """
    key = generate_keypair()
    manifest = BundleManifest(
        format_version=1,
        module="browser",
        version="1.0.0",
        issuer="did:arc:release",
        files=[FileEntry(path="_runtime.py", sha256="a" * 64)],
    )

    signature = sign_manifest(manifest, private_key=key.private_key, issuer="did:arc:release")

    assert len(signature) == 64
    assert verify(manifest.canonical_bytes(), signature, key.public_key)


def _craft(bundle_root: Path, *, hostile_path: str, hostile_file: Path) -> bytes:
    """Write a validly signed manifest whose single entry escapes the bundle.

    Built from a raw mapping rather than the model, because the model is exactly
    the thing under test — it must be the gate that refuses this shape.
    """
    hostile_file.parent.mkdir(parents=True, exist_ok=True)
    hostile_file.write_bytes(_HOSTILE_CONTENT)

    key = generate_keypair()
    mapping = {
        "files": [
            {"path": hostile_path, "sha256": hashlib.sha256(_HOSTILE_CONTENT).hexdigest()},
        ],
        "format_version": 1,
        "issuer": "did:arc:release",
        "module": "browser",
        "version": "1.0.0",
    }
    raw = canonical_json(mapping)
    (bundle_root / "files").mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.json").write_bytes(raw)
    (bundle_root / "manifest.sig").write_bytes(sign(raw, key.private_key))
    return key.public_key


@pytest.mark.parametrize(
    "hostile_path",
    ["../escaped.py", "tools/../../escaped.py", "./../escaped.py"],
)
def test_relative_traversal_in_a_manifest_entry_is_refused(
    tmp_path: Path,
    dest_root: Path,
    snapshot_tree: Snapshot,
    hostile_path: str,
) -> None:
    """`../` in a declared path is rejected before anything is written."""
    bundle_root = tmp_path / "browser.bundle"
    escaped = Path(os.path.normpath(bundle_root / "files" / hostile_path))
    assert bundle_root / "files" not in escaped.parents, "the fixture does not actually escape"
    public_key = _craft(bundle_root, hostile_path=hostile_path, hostile_file=escaped)
    before = snapshot_tree(dest_root)

    with pytest.raises(BundleManifestError):
        verify_bundle(
            bundle_root, tier="personal", trusted_issuers={"did:arc:release": public_key}
        )

    assert snapshot_tree(dest_root) == before


def test_absolute_path_in_a_manifest_entry_is_refused(
    tmp_path: Path,
    dest_root: Path,
    snapshot_tree: Snapshot,
) -> None:
    """An absolute path is the same escape without the `../` spelling."""
    bundle_root = tmp_path / "browser.bundle"
    outside = tmp_path / "outside" / "escaped.py"
    public_key = _craft(bundle_root, hostile_path=str(outside), hostile_file=outside)
    before = snapshot_tree(dest_root)

    with pytest.raises(BundleManifestError):
        verify_bundle(
            bundle_root, tier="personal", trusted_issuers={"did:arc:release": public_key}
        )

    assert snapshot_tree(dest_root) == before


def test_the_model_itself_refuses_a_traversing_file_entry() -> None:
    """The check lives on the model, so no caller can construct one at all."""
    with pytest.raises(BundleManifestError):
        FileEntry(path="../escaped.py", sha256="a" * 64)
