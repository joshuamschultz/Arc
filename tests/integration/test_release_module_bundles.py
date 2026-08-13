"""Release CI really produces installable, verifiable module bundles.

SPEC-066 T-970 (REQ-336). The wheel ships no module code, so the release job that
signs and publishes the bundles IS the distribution channel. A broken signing
step would not fail loudly — it would publish artifacts that every deployment
refuses, which looks like a verification bug on eighteen boxes rather than a
build bug on one.

So this drives ``scripts/build_module_bundles.py`` for real with a throwaway
release key and pushes its output back through ``arcbundle.verify_bundle`` — at
**federal** tier, the strictest one, because a release bundle that only verifies
at personal is not a release bundle.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import arcbundle
from arctrust import KeyPair, generate_keypair

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "build_module_bundles.py"
_CATALOG = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent" / "modules"


def _run_script(out_dir: Path, keypair: KeyPair, issuer: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ARC_RELEASE_SIGNING_KEY": base64.b64encode(keypair.private_key).decode("ascii"),
        "ARC_RELEASE_ISSUER_DID": issuer,
    }
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--out", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        check=False,
    )


def _catalog_names() -> set[str]:
    return {
        path.name
        for path in _CATALOG.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    }


def test_every_catalog_module_becomes_a_bundle_that_verifies_at_federal(tmp_path: Path) -> None:
    """One signed, publishable asset per module — each accepted by the real verifier."""
    keypair = generate_keypair()
    issuer = "did:arc:release:test"

    result = _run_script(tmp_path, keypair, issuer)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    expected = _catalog_names()
    assert expected, "no modules in the catalog — this test would prove nothing"

    for name in sorted(expected):
        bundle = tmp_path / f"{name}.arcbundle"
        verified = arcbundle.verify_bundle(
            bundle,
            tier="federal",
            trusted_issuers={issuer: keypair.public_key},
        )
        assert verified.manifest.module == name
        assert verified.files, f"{name} bundle declares no files"

    # Release assets are files; a bundle is a directory. Each one is archived,
    # and the archive has to expand back into the exact directory `arc module
    # install --from` reads, or the published asset is not installable.
    for name in sorted(expected):
        archive = tmp_path / f"{name}.arcbundle.tar.gz"
        assert archive.is_file()
        with tarfile.open(archive) as tar:
            members = tar.getnames()
        assert f"{name}.arcbundle/{arcbundle.MANIFEST_NAME}" in members
        assert f"{name}.arcbundle/{arcbundle.SIGNATURE_NAME}" in members


def test_the_script_refuses_to_build_anything_unsigned(tmp_path: Path) -> None:
    """A missing key must fail the release job, not quietly emit unsigned bundles.

    An unsigned bundle verifies nowhere, so shipping one turns a CI
    misconfiguration into eighteen deployments that all refuse to install.
    """
    env = {k: v for k, v in os.environ.items() if k != "ARC_RELEASE_SIGNING_KEY"}
    env["ARC_RELEASE_ISSUER_DID"] = "did:arc:release:test"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "ARC_RELEASE_SIGNING_KEY" in result.stderr
    assert not list(tmp_path.iterdir())


def test_the_script_refuses_to_sign_a_release_with_the_development_issuer(
    tmp_path: Path,
) -> None:
    """`did:arc:dev` is trusted at personal tier only. A release signed with it
    would be a release no enterprise or federal box could ever install."""
    result = _run_script(tmp_path, generate_keypair(), arcbundle.DEV_ISSUER)

    assert result.returncode == 1
    assert arcbundle.DEV_ISSUER in result.stderr
    assert not list(tmp_path.iterdir())
