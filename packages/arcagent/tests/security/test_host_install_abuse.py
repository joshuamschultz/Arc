"""Abuse cases for the Install button (alpha-2 J1-3): supply chain on the host.

The button downloads a helper program and puts it on the operator's machine, so
every property below is an attacker's route to running their code there:

* a download whose sha256 is not the pinned one is refused and nothing lands;
* an npm tarball carrying a path-traversal entry is refused before npm sees it;
* a package's ``postinstall`` script never runs (``--ignore-scripts``), proven
  against the real npm, not only against the argv;
* a federal deployment does not fetch from the network unless the digest is on
  its allowlist.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.host_install import _npm_argv, install_pinned_binary, run_npm
from arcagent.extension.manifest import ArtifactPin, PlatformArtifact
from arcagent.extension.platforms import ANY_PLATFORM

_CALLER = "did:arc:test-operator"
_URL = "https://registry.invalid/@acme/cli/-/cli-1.0.0.tgz"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _tarball(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


_GOOD = {
    "package/package.json": b'{"name":"@acme/cli","version":"1.0.0","bin":{"acme":"./cli.js"}}',
    "package/cli.js": b"#!/usr/bin/env node\n",
}


def _pin(payload: bytes, digest: str | None = None) -> ArtifactPin:
    return ArtifactPin(
        package="@acme/cli",
        version="1.0.0",
        platforms={
            ANY_PLATFORM: PlatformArtifact(
                url=_URL, sha256=digest or hashlib.sha256(payload).hexdigest()
            )
        },
    )


class _Npm:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, argv: Any) -> tuple[int, str]:
        self.calls.append(argv)
        return 0, ""


async def _attempt(
    payload: bytes, tmp_path: Path, pin: ArtifactPin, npm: _Npm, tier: Tier = Tier.PERSONAL
) -> Path:
    async def fetch(_url: str) -> bytes:
        return payload

    return await install_pinned_binary(
        pin,
        install_dir=tmp_path / "bin",
        caller_did=_CALLER,
        audit_sink=_Sink(),
        tier=tier,
        fetch=fetch,
        npm_run=npm,
        which=lambda name: f"/usr/bin/{name}",
    )


@pytest.fixture(autouse=True)
def _operator_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "operator"
    monkeypatch.setenv("ARC_TEAM_ROOT", str(root))
    return root


async def test_a_swapped_download_is_refused_and_nothing_is_installed(
    tmp_path: Path, _operator_root: Path
) -> None:
    approved = _tarball(_GOOD)
    swapped = _tarball({**_GOOD, "package/cli.js": b"#!/usr/bin/env node\nevil()\n"})
    npm = _Npm()

    with pytest.raises(ExtensionError) as refused:
        await _attempt(swapped, tmp_path, _pin(approved), npm)

    assert refused.value.details["reason"] == "hash_mismatch"
    assert npm.calls == []
    assert not (tmp_path / "bin").exists()
    assert not (_operator_root / "host-tools").exists()


@pytest.mark.parametrize("entry", ["package/../../../evil.js", "/etc/cron.d/evil"])
async def test_a_path_traversal_entry_is_refused_before_npm_runs(
    entry: str, tmp_path: Path
) -> None:
    payload = _tarball({**_GOOD, entry: b"x"})
    npm = _Npm()

    with pytest.raises(ExtensionError) as refused:
        await _attempt(payload, tmp_path, _pin(payload), npm)

    assert refused.value.details["reason"] == "path_traversal"
    assert npm.calls == []


async def test_a_postinstall_script_never_runs(tmp_path: Path) -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm is not installed on this machine")
    marker = tmp_path / "pwned"
    evil = {
        "package/package.json": (
            b'{"name":"evil","version":"1.0.0","bin":{"evil":"./x.js"},'
            b'"scripts":{"postinstall":"touch ' + str(marker).encode() + b'"}}'
        ),
        "package/x.js": b"#!/usr/bin/env node\n",
    }
    tarball = tmp_path / "evil.tgz"
    tarball.write_bytes(_tarball(evil))

    code, output = await run_npm(_npm_argv(tmp_path / "prefix", tarball))

    assert code == 0, output
    assert not marker.exists()


async def test_federal_does_not_fetch_a_digest_the_operator_never_allowlisted(
    tmp_path: Path,
) -> None:
    payload = _tarball(_GOOD)
    fetched: list[str] = []

    async def fetch(url: str) -> bytes:
        fetched.append(url)
        return payload

    with pytest.raises(ExtensionError) as refused:
        await install_pinned_binary(
            _pin(payload),
            install_dir=tmp_path / "bin",
            caller_did=_CALLER,
            audit_sink=_Sink(),
            tier=Tier.FEDERAL,
            fetch=fetch,
        )

    assert refused.value.details["reason"] == "federal_not_allowlisted"
    assert fetched == []
