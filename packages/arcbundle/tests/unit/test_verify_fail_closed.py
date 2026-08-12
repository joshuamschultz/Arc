"""T-959 (RED) — REQ-326 / REQ-327 / REQ-331: verification is fail-closed, and a
refusal leaves the destination byte-identical to what it was.

"Nothing is written before everything is verified" is the whole reason bundles
exist. A partial tree is worse than no install: half a module on disk at 0444 is
indistinguishable, on a filesystem listing, from a module the deployment was
approved for — which defeats the one property an operator is supposed to be able
to point at.

Each case here does the real two-step an install does — verify, then materialize
— against a destination that already holds an installed module, and asserts the
destination is unchanged to the byte afterwards.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from arctrust import generate_keypair
from packages.arcbundle.tests.conftest import BundleFactory

from arcbundle import (
    DEV_ISSUER,
    BundleContentHashError,
    BundleSignatureError,
    materialize,
    verify_bundle,
)

Snapshot = Callable[[Path], dict[str, bytes | None]]


def _install(bundle: Path, dest: Path, *, tier: str, trusted: dict[str, bytes]) -> Path:
    """The install path in miniature: verify first, write only after.

    The CLI's non-zero exit is this exception surfacing; the library contract is
    that the raise happens before any byte lands.
    """
    verified = verify_bundle(bundle, tier=tier, trusted_issuers=trusted)
    return materialize(verified, dest)


def test_tampered_manifest_is_refused_and_writes_nothing(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
) -> None:
    """A manifest edited after signing no longer matches its signature."""
    bundle = make_bundle(tmp_path / "browser.bundle")
    before = snapshot_tree(dest_root)

    tampered = bundle.manifest_json.read_bytes().replace(b'"1.0.0"', b'"9.9.9"')
    assert tampered != bundle.manifest_json.read_bytes(), "the edit did not change the manifest"
    bundle.manifest_json.write_bytes(tampered)

    with pytest.raises(BundleSignatureError):
        _install(bundle.path, dest_root, tier="personal", trusted=bundle.trusted())

    assert snapshot_tree(dest_root) == before
    assert not (dest_root / "browser").exists()


def test_tampered_payload_file_is_refused_and_writes_nothing(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
) -> None:
    """A payload swapped under an intact signature fails on its content hash.

    This is the case a signature alone does not catch: the manifest is genuine,
    so only the per-file digest stands between the deployment and a swapped
    module runtime.
    """
    bundle = make_bundle(tmp_path / "browser.bundle")
    before = snapshot_tree(dest_root)

    bundle.payload_file("_runtime.py").write_bytes(b"import os\nos.system('curl evil')\n")

    with pytest.raises(BundleContentHashError):
        _install(bundle.path, dest_root, tier="personal", trusted=bundle.trusted())

    assert snapshot_tree(dest_root) == before
    assert not (dest_root / "browser").exists()


def test_materialize_interrupted_mid_write_leaves_the_destination_untouched(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durability failure partway through the write rolls the whole thing back.

    The interruption is injected at `os.fsync`, the point the SDD puts between
    staging and rename: staging has already written bytes somewhere, and the
    rename that would publish them has not happened. A stage-then-rename
    materializer survives this with the destination untouched and no temp tree
    left behind; a write-in-place one does not.
    """
    bundle = make_bundle(tmp_path / "browser.bundle")
    before = snapshot_tree(dest_root)

    def _interrupt(fd: int) -> None:
        raise OSError(errno.EIO, "simulated interruption mid-materialize")

    monkeypatch.setattr(os, "fsync", _interrupt)

    with pytest.raises(OSError):
        _install(bundle.path, dest_root, tier="personal", trusted=bundle.trusted())

    assert snapshot_tree(dest_root) == before
    assert not (dest_root / "browser").exists()


def test_dev_signed_bundle_installs_at_personal_and_is_refused_above_it(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
) -> None:
    """The development key is a personal-tier convenience, not a release path.

    Both halves are asserted so the test pins a boundary rather than a blanket
    failure: the same bundle, the same trusted-issuer mapping, the same call —
    only the tier differs. The check belongs in the verifier, so no CLI flag,
    no direct API call, and no future caller can route around it.
    """
    dev_key = generate_keypair()
    bundle = make_bundle(tmp_path / "browser.bundle", issuer=DEV_ISSUER, keypair=dev_key)
    trusted = {DEV_ISSUER: dev_key.public_key}

    installed = _install(bundle.path, dest_root, tier="personal", trusted=trusted)
    assert (installed / "_runtime.py").read_bytes() == bundle.payload["_runtime.py"]
    assert (installed / "tools" / "fetch.py").read_bytes() == bundle.payload["tools/fetch.py"]

    for tier in ("enterprise", "federal"):
        hardened_dest = tmp_path / f"modules-{tier}"
        hardened_dest.mkdir()
        before = snapshot_tree(hardened_dest)

        with pytest.raises(BundleSignatureError):
            _install(bundle.path, hardened_dest, tier=tier, trusted=trusted)

        assert snapshot_tree(hardened_dest) == before
