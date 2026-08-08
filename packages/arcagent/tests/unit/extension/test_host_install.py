"""SPEC-064 — ``install_pinned_binary``: verified bytes, or nothing lands at all.

:mod:`arcagent.extension.host` states the rule this module lives inside — Arc
never RUNS a manifest's install instruction — and these tests pin the version of
that rule an installer has to keep:

* **Nothing is executed.** Asserted structurally, off the module's own imports,
  for the same reason ``test_pin`` asserts it there: a component that could spawn
  a process could turn a declared manifest string into a command on the host with
  no operator in the loop.
* **The digest is checked before anything is unpacked**, and a mismatch leaves
  the install directory exactly as it was. Asserted on the directory contents,
  not on the exception: a refusal that raises after writing is a refusal an
  operator has to clean up.
* **A host with no pinned digest is refused before a byte is fetched.** The
  fetcher is a spy, so "did not download" is an assertion rather than an
  assumption — this is the case where skipping verification would be tempting.
* **Nothing is ever written outside the install directory.** A manifest is
  adversarial input and its ``member`` is a path; the archive member is placed by
  its basename alone, so a traversal has nowhere to go.

Every refusal is asserted against the audit event that reached the sink, not the
exception text: an auditor reconstructs a supply-chain refusal from the chain.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import os
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.host_install import install_pinned_binary
from arcagent.extension.manifest import ArtifactPin, PlatformArtifact
from arcagent.extension.platforms import ANY_PLATFORM, host_platform

_CALLER = "did:arc:test-caller"

#: Anything that could turn "place these bytes" into "run these bytes".
_EXEC_CAPABLE = frozenset({"subprocess", "pty", "runpy", "importlib"})

#: The payload the fixture archives carry, so a test can prove the RIGHT member
#: was extracted rather than merely that a file appeared.
_BINARY = b"#!/bin/sh\necho acme 1.0.0\n"

_MEMBER = "acme_1.0.0_dist/bin/acme"


class _RecordingSink:
    """An audit sink that keeps what it was handed, so tests read the real event."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _SpyFetcher:
    """A fetcher that records every URL it was asked for and never touches a network."""

    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.urls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return self.payload


def _tarball(member: str = _MEMBER, body: bytes = _BINARY) -> bytes:
    """A gzip tarball holding one member, built the way a release asset is."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(body)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _zipball(member: str = _MEMBER, body: bytes = _BINARY) -> bytes:
    """The same, as a zip — ``gh`` publishes its macOS builds this way."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr(member, body)
    return buffer.getvalue()


def _pin(
    payload: bytes,
    *,
    url: str = "https://example.invalid/acme_1.0.0_dist.tar.gz",
    member: str = _MEMBER,
    platform: str | None = None,
    digest: str | None = None,
) -> ArtifactPin:
    """A pin covering this host, matching ``payload`` unless a test breaks it."""
    return ArtifactPin(
        package="acme/acme",
        version="1.0.0",
        platforms={
            platform or host_platform(): PlatformArtifact(
                url=url,
                sha256=digest or hashlib.sha256(payload).hexdigest(),
                member=member,
            )
        },
    )


async def _install(pin: ArtifactPin, install_dir: Path, fetch: Any) -> Path:
    return await install_pinned_binary(
        pin,
        install_dir=install_dir,
        caller_did=_CALLER,
        audit_sink=fetch.sink,
        tier=Tier.PERSONAL,
        fetch=fetch,
    )


class _Fetch(_SpyFetcher):
    """A spy fetcher carrying the sink its install records into, to keep call sites short."""

    def __init__(self, payload: bytes = b"") -> None:
        super().__init__(payload)
        self.sink = _RecordingSink()

    def denials(self) -> list[AuditEvent]:
        return [event for event in self.sink.events if event.outcome == "deny"]


# --- the verified build lands ------------------------------------------------


async def test_a_verified_tarball_member_is_installed_and_executable(tmp_path: Path) -> None:
    payload = _tarball()
    fetch = _Fetch(payload)
    install_dir = tmp_path / "bin"

    path = await _install(_pin(payload), install_dir, fetch)

    assert path == install_dir / "acme"
    assert path.read_bytes() == _BINARY
    assert path.stat().st_mode & 0o111, "an installed binary nobody can run is not installed"


async def test_a_verified_zip_member_is_installed(tmp_path: Path) -> None:
    """``gh`` ships its macOS builds as a zip; the same pin must cover both shapes."""
    payload = _zipball()
    fetch = _Fetch(payload)

    path = await _install(
        _pin(payload, url="https://example.invalid/acme_1.0.0_macOS_arm64.zip"),
        tmp_path / "bin",
        fetch,
    )

    assert path.read_bytes() == _BINARY


async def test_a_completed_install_is_recorded_with_the_digest_that_was_checked(
    tmp_path: Path,
) -> None:
    """Pillar 4 has no happy-path exemption: an auditor sees what was allowed to land."""
    payload = _tarball()
    fetch = _Fetch(payload)

    await _install(_pin(payload), tmp_path / "bin", fetch)

    (event,) = fetch.sink.events
    assert event.outcome == "allow"
    assert event.actor_did == _CALLER
    assert event.extra["sha256"] == hashlib.sha256(payload).hexdigest()


# --- a digest that does not match is a hard refusal --------------------------


async def test_a_digest_mismatch_refuses_and_installs_nothing(tmp_path: Path) -> None:
    """The whole point: bytes that are not the approved bytes never reach the disk."""
    fetch = _Fetch(_tarball())
    install_dir = tmp_path / "bin"

    with pytest.raises(ExtensionError):
        await _install(_pin(b"different bytes entirely"), install_dir, fetch)

    assert not install_dir.exists() or list(install_dir.iterdir()) == []


async def test_a_digest_mismatch_names_the_expected_and_the_actual_digest(
    tmp_path: Path,
) -> None:
    payload = _tarball()
    fetch = _Fetch(payload)
    expected = hashlib.sha256(b"different bytes entirely").hexdigest()

    with pytest.raises(ExtensionError):
        await _install(_pin(payload, digest=expected), tmp_path / "bin", fetch)

    (event,) = fetch.denials()
    assert event.extra["reason"] == "hash_mismatch"
    assert event.extra["expected"] == expected
    assert event.extra["actual"] == hashlib.sha256(payload).hexdigest()


# --- an unpinned platform is refused before anything is fetched --------------


async def test_a_platform_with_no_pinned_digest_refuses_without_downloading(
    tmp_path: Path,
) -> None:
    """The tempting repair here is "fetch it and install it unverified". There is no path to it."""
    fetch = _Fetch(_tarball())

    with pytest.raises(ExtensionError):
        await _install(_pin(b"", platform="sunos/sparc"), tmp_path / "bin", fetch)

    assert fetch.urls == [], "a refused platform must not reach the network at all"
    (event,) = fetch.denials()
    assert event.extra["reason"] == "unpinned_platform"
    assert host_platform() in event.extra["expected"]


async def test_a_pin_with_no_installable_member_refuses_without_downloading(
    tmp_path: Path,
) -> None:
    """An npm tarball or a PyPI sdist is pinned and still is not a binary to place.

    Saying so is the honest answer; downloading it and dropping the tarball into
    ``~/.local/bin`` would be a file that looks installed and runs nothing.
    """
    fetch = _Fetch(b"")

    with pytest.raises(ExtensionError):
        await _install(_pin(b"", member="", platform=ANY_PLATFORM), tmp_path / "bin", fetch)

    assert fetch.urls == []
    assert fetch.denials()[0].extra["reason"] == "not_a_binary"


# --- the install directory is the only place anything is written -------------


async def test_a_traversing_member_lands_inside_the_install_directory(tmp_path: Path) -> None:
    """A manifest is adversarial input and ``member`` is a path it chose.

    The member is placed by its basename, so ``../../../../etc/passwd`` is
    ``passwd`` inside the install directory and there is no arrangement of dots
    that reaches a parent.
    """
    payload = _tarball(member="../../../../etc/passwd")
    fetch = _Fetch(payload)
    install_dir = tmp_path / "bin"
    outside = tmp_path / "etc"

    path = await _install(_pin(payload, member="../../../../etc/passwd"), install_dir, fetch)

    assert path.parent == install_dir
    assert not outside.exists()


async def test_a_member_the_archive_does_not_hold_is_refused(tmp_path: Path) -> None:
    payload = _tarball(member="acme_1.0.0_dist/bin/somethingelse")
    fetch = _Fetch(payload)
    install_dir = tmp_path / "bin"

    with pytest.raises(ExtensionError):
        await _install(_pin(payload), install_dir, fetch)

    assert not install_dir.exists() or list(install_dir.iterdir()) == []
    assert fetch.denials()[0].extra["reason"] == "member_missing"


async def test_an_archive_shape_the_installer_cannot_read_is_refused(tmp_path: Path) -> None:
    """Verified bytes that are not an archive are still not something to place blindly."""
    payload = b"not an archive"
    fetch = _Fetch(payload)

    with pytest.raises(ExtensionError):
        await _install(
            _pin(payload, url="https://example.invalid/acme.tar.gz"), tmp_path / "bin", fetch
        )

    assert fetch.denials()[0].extra["reason"] == "unreadable_archive"


async def test_an_existing_binary_is_replaced_rather_than_left_stale(tmp_path: Path) -> None:
    """Re-running setup after a failed attempt must end with the verified bytes."""
    payload = _tarball()
    fetch = _Fetch(payload)
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    (install_dir / "acme").write_bytes(b"an older build")

    path = await _install(_pin(payload), install_dir, fetch)

    assert path.read_bytes() == _BINARY


async def test_the_installed_binary_is_not_group_or_world_writable(tmp_path: Path) -> None:
    """Anyone who can rewrite it can choose what the agent executes next."""
    payload = _tarball()
    fetch = _Fetch(payload)

    path = await _install(_pin(payload), tmp_path / "bin", fetch)

    assert not path.stat().st_mode & (0o020 | 0o002)


# --- structural: this installer cannot become a command runner ---------------


def _module() -> ModuleType:
    import arcagent.extension.host_install as module

    return module


def test_the_installer_imports_nothing_that_could_execute_what_it_installs() -> None:
    """REQ-262: a machine-level change stays visible and stays the operator's.

    An installer able to spawn a process could run the manifest's ``instruction``
    — which is the exact hole ``arcagent.extension.host`` exists to keep shut.
    """
    tree = ast.parse(inspect.getsource(_module()))
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert imported & _EXEC_CAPABLE == set()


def test_the_default_install_directory_is_user_writable_and_never_a_system_path() -> None:
    """Never sudo, never ``/usr/local/bin``: an install needing root is one an
    operator did not authorise at the moment it happened."""
    from arcagent.extension.host_install import host_install_dir

    target = host_install_dir()

    assert target == Path.home() / ".local" / "bin"
    assert not str(target).startswith(("/usr", "/opt", "/bin", "/sbin"))
    assert os.access(target.parent.parent, os.W_OK)
