"""Reinstall over an existing module — the branch a first install never runs.

`materialize` is not only an install; it is a *replace*. `os.replace` refuses a
non-empty directory target, so a second install has to vacate the path first,
and `_displace` does it by rename precisely so the old tree stays recoverable
until the new one is published.

Nothing exercised that. Every other test in this package installs once, into a
path that does not yet hold the module, so `_displace` returns ``None`` and the
rollback arm of the publishing rename is unreachable. Two cases here close it:

* the reinstall succeeds — new bytes on disk, old tree gone, no litter left;
* the publishing rename fails — the *previous* install comes back byte for
  byte, the exception still reaches the caller, and no litter is left.

The second is the one the module's docstring promises and the one an operator
depends on: a failed upgrade must leave the module that was working in place,
not a hole where it used to be.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from packages.arcbundle.tests.conftest import BundleFactory

from arcbundle import materialize, verify_bundle

Snapshot = Callable[[Path], dict[str, bytes | None]]

# Distinct on every path, and v2 drops a file v1 had: a reinstall that merged
# trees instead of replacing one would leave `legacy.py` behind and pass a
# content check that only looked at the files v2 declares.
_V1: Mapping[str, bytes] = {
    "_runtime.py": b"VERSION = '1.0.0'\n",
    "legacy.py": b"# removed in 2.0.0\n",
    "tools/fetch.py": b"def fetch():\n    return 1\n",
}
_V2: Mapping[str, bytes] = {
    "_runtime.py": b"VERSION = '2.0.0'\n",
    "tools/fetch.py": b"def fetch():\n    return 2\n",
    "skills/SKILL.md": b"# fetch\n",
}

# The staging tree is the source of exactly one of the three renames a reinstall
# performs, which is what makes it the handle for injecting a publish failure.
_STAGING_MARK = ".staging-"
_BACKUP_MARK = ".backup-"


def _install(bundle: Path, dest: Path, *, trusted: dict[str, bytes]) -> Path:
    verified = verify_bundle(bundle, tier="personal", trusted_issuers=trusted)
    return materialize(verified, dest)


def _litter(dest: Path) -> list[str]:
    """Temporary trees `materialize` created and was supposed to clean up."""
    return sorted(
        entry.name
        for entry in dest.iterdir()
        if _STAGING_MARK in entry.name or _BACKUP_MARK in entry.name
    )


def test_reinstalling_a_module_replaces_it_and_leaves_no_litter(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
) -> None:
    """The second install displaces the first and cleans up after itself."""
    trusted_key = make_bundle(tmp_path / "v1.bundle", version="1.0.0", payload=_V1)
    first = _install(trusted_key.path, dest_root, trusted=trusted_key.trusted())
    assert (first / "_runtime.py").read_bytes() == _V1["_runtime.py"]

    second_bundle = make_bundle(
        tmp_path / "v2.bundle",
        version="2.0.0",
        issuer=trusted_key.issuer,
        keypair=trusted_key.keypair,
        payload=_V2,
    )
    second = _install(second_bundle.path, dest_root, trusted=second_bundle.trusted())

    assert second == first
    assert (second / "_runtime.py").read_bytes() == _V2["_runtime.py"]
    assert (second / "tools" / "fetch.py").read_bytes() == _V2["tools/fetch.py"]
    assert (second / "skills" / "SKILL.md").read_bytes() == _V2["skills/SKILL.md"]
    assert not (second / "legacy.py").exists(), "the v1 tree was merged, not replaced"

    assert _litter(dest_root) == []
    assert sorted(entry.name for entry in dest_root.iterdir()) == ["browser", "existing"]


def test_failed_publish_rename_restores_the_previous_install_byte_for_byte(
    tmp_path: Path,
    dest_root: Path,
    make_bundle: BundleFactory,
    snapshot_tree: Snapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rename that fails after the old tree moved aside puts the old tree back.

    The failure is injected at the one rename that publishes — staging into the
    module path — and only there, so the rename that vacated the path and the
    rename that undoes it both run for real. That is the exact window the
    rollback exists for: the previous install is no longer where it lived, the
    new one never arrived, and something has to put it back before the exception
    leaves the frame.
    """
    first_bundle = make_bundle(tmp_path / "v1.bundle", version="1.0.0", payload=_V1)
    _install(first_bundle.path, dest_root, trusted=first_bundle.trusted())
    before = snapshot_tree(dest_root)

    second_bundle = make_bundle(
        tmp_path / "v2.bundle",
        version="2.0.0",
        issuer=first_bundle.issuer,
        keypair=first_bundle.keypair,
        payload=_V2,
    )

    real_replace = os.replace

    def _fail_only_the_publish(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if _STAGING_MARK in os.fspath(src):
            raise OSError(errno.EIO, "simulated publish failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_only_the_publish)

    # The match pins the injection: if the publish rename stopped being the one
    # renaming out of a staging tree, nothing would raise and this would fail
    # rather than quietly stop testing the rollback.
    with pytest.raises(OSError, match="simulated publish failure"):
        _install(second_bundle.path, dest_root, trusted=second_bundle.trusted())

    monkeypatch.undo()

    assert snapshot_tree(dest_root) == before
    assert (dest_root / "browser" / "_runtime.py").read_bytes() == _V1["_runtime.py"]
    assert (dest_root / "browser" / "legacy.py").read_bytes() == _V1["legacy.py"]
    assert not (dest_root / "browser" / "skills").exists()
    assert _litter(dest_root) == []
