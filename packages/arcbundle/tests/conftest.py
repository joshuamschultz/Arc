"""Fixtures for arcbundle tests — real Ed25519 keys, real bundles on disk.

Every fixture builds artifacts the way the shipping path will: a payload tree
under ``files/``, a canonical-JSON ``manifest.json``, and a detached
``manifest.sig`` beside it. Nothing is mocked, so a test that passes here has
exercised the same bytes an operator would carry on approved media.

The ``arcbundle`` imports are deliberately *inside* the factory body. These
tests are written before the implementation exists (T-956 / T-959, SPEC-066),
and a module-level import of an absent symbol in a conftest aborts collection
for the entire tree — which hides what is missing instead of showing it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest
from arctrust import KeyPair, generate_keypair

# A nested path is in the default payload on purpose: materialize has to create
# intermediate directories, and a flat payload would never prove it.
_DEFAULT_PAYLOAD: dict[str, bytes] = {
    "_runtime.py": b"def configure(agent):\n    return None\n",
    "tools/fetch.py": b"def fetch(url: str) -> str:\n    return url\n",
}

_RELEASE_ISSUER = "did:arc:release"


@dataclass(frozen=True)
class BundleFixture:
    """A signed bundle on disk plus everything a test needs to verify it."""

    path: Path
    issuer: str
    keypair: KeyPair
    payload: dict[str, bytes]

    @property
    def manifest_json(self) -> Path:
        return self.path / "manifest.json"

    @property
    def signature(self) -> Path:
        return self.path / "manifest.sig"

    def payload_file(self, relative: str) -> Path:
        return self.path / "files" / relative

    def trusted(self) -> dict[str, bytes]:
        """The trusted-issuer mapping under which this bundle is legitimate."""
        return {self.issuer: self.keypair.public_key}


class BundleFactory(Protocol):
    """Builds a signed bundle rooted at ``root``."""

    def __call__(
        self,
        root: Path,
        *,
        module: str = ...,
        version: str = ...,
        issuer: str = ...,
        keypair: KeyPair | None = ...,
        payload: Mapping[str, bytes] | None = ...,
    ) -> BundleFixture: ...


@pytest.fixture
def make_bundle() -> BundleFactory:
    """Write a valid, correctly signed bundle and return a handle to it."""

    def _make(
        root: Path,
        *,
        module: str = "browser",
        version: str = "1.0.0",
        issuer: str = _RELEASE_ISSUER,
        keypair: KeyPair | None = None,
        payload: Mapping[str, bytes] | None = None,
    ) -> BundleFixture:
        from arcbundle import BundleManifest, FileEntry, sign_manifest

        signing_key = keypair if keypair is not None else generate_keypair()
        content = dict(payload) if payload is not None else dict(_DEFAULT_PAYLOAD)

        entries = []
        for relative in sorted(content):
            target = root / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content[relative])
            entries.append(
                FileEntry(path=relative, sha256=hashlib.sha256(content[relative]).hexdigest())
            )

        manifest = BundleManifest(
            format_version=1,
            module=module,
            version=version,
            issuer=issuer,
            files=entries,
        )
        (root / "manifest.json").write_bytes(manifest.canonical_bytes())
        (root / "manifest.sig").write_bytes(
            sign_manifest(manifest, private_key=signing_key.private_key, issuer=issuer)
        )
        return BundleFixture(path=root, issuer=issuer, keypair=signing_key, payload=content)

    return _make


@pytest.fixture
def dest_root(tmp_path: Path) -> Path:
    """A deployment module root that already holds an installed module.

    Pre-populated so "the destination is unchanged" is a real claim about
    existing bytes rather than a claim about an empty directory.
    """
    root = tmp_path / "modules"
    (root / "existing" / "sub").mkdir(parents=True)
    (root / "existing" / "_runtime.py").write_bytes(b"# already installed\n")
    (root / "existing" / "sub" / "data.txt").write_bytes(b"payload\n")
    return root


@pytest.fixture
def snapshot_tree() -> Callable[[Path], dict[str, bytes | None]]:
    """Capture a directory tree as ``relative path -> file bytes`` (None = dir)."""

    def _snapshot(root: Path) -> dict[str, bytes | None]:
        captured: dict[str, bytes | None] = {}
        for entry in sorted(root.rglob("*")):
            key = str(entry.relative_to(root))
            captured[key] = entry.read_bytes() if entry.is_file() else None
        return captured

    return _snapshot
