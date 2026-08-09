"""SPEC-064 — the verified host install: pinned bytes, checked before they land.

:mod:`arcagent.extension.host` holds the rule this module lives inside — Arc
DIRECTS a host install and never runs the manifest's instruction — and nothing
here breaks it. A manifest names *bytes*, not a command: a URL, and the sha256
those bytes must hash to **for this platform**. This module fetches them, hashes
them, and only then unpacks one declared member into a user-writable directory.
There is no shell, no subprocess, and no path by which a manifest string becomes
something that executes.

Three properties are the whole of it:

* **The digest is resolved for THIS host, or the install refuses.** One sha256
  standing for every platform was the state that made a button impossible: it
  matched the machine it was taken from and described unrelated bytes everywhere
  else, leaving only two bad options — skip verification, or refuse every host
  but one. A platform the pin does not name is refused before a byte is fetched.
* **Verification happens before anything is unpacked**, so a refusal leaves the
  install directory exactly as it was. A mismatch is a hard refusal and an audit
  event; there is no warn-and-continue.
* **``~/.local/bin``, never a system path and never with sudo.** An install
  needing root is a machine-level change the operator did not authorise at the
  moment it happened. The archive member is placed by its BASENAME, so a manifest
  choosing ``../../../../etc/passwd`` writes ``passwd`` inside the install
  directory and reaches no parent.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import NoReturn

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.manifest import ArtifactPin, PlatformArtifact
from arcagent.extension.platforms import host_platform

#: How a URL becomes bytes. Injected so a test never depends on a network, for the
#: same reason ``HostPrerequisiteDirector`` injects its path lookup.
Fetcher = Callable[[str], Awaitable[bytes]]

#: A published CLI release is tens of megabytes; anything past this is not one, and
#: an unbounded read of an operator-supplied URL is a memory exhaustion (LLM10).
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024

#: How long a download may take before the operator is told it did not happen.
_FETCH_TIMEOUT_SECONDS = 300.0

#: Owner-writable, world-executable. Anyone who can rewrite the file chooses what
#: the agent runs next, so the group and other write bits are never set.
_BINARY_MODE = 0o755

#: URL endings that mean the download wraps the executable rather than being it.
_ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar", ".tar.xz", ".tar.bz2")

_ACTION = "extension.host.install"

_REFUSED = "HOST_INSTALL_REFUSED"


def host_install_dir() -> Path:
    """Where a verified host binary lands: the operator's own ``~/.local/bin``.

    Resolved per call rather than at import so a deployment that redirects ``HOME``
    gets the directory that deployment actually writes to.
    """
    return Path.home() / ".local" / "bin"


async def install_pinned_binary(
    pin: ArtifactPin,
    *,
    install_dir: Path,
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    fetch: Fetcher | None = None,
) -> Path:
    """Put one pinned build on this host, or refuse without writing anything.

    Args:
        pin: The manifest's ``[artifact]`` table, carrying a digest per platform.
        install_dir: The user-writable directory the binary is placed in. Nothing
            is ever written outside it.
        caller_did: The operator recorded as the actor on the verdict (Pillar 1).
        audit_sink: Where the verdict is recorded — before it is raised.
        tier: Deployment stringency, stamped on the record. It selects no
            behaviour: a build nobody approved is not more acceptable on a laptop.
        fetch: How the URL becomes bytes. Defaults to a bounded HTTPS GET.

    Returns:
        The path of the installed binary.

    Raises:
        ExtensionError: This host has no pinned digest, the pin covers no binary
            to place, the download failed, the bytes did not match the digest, or
            the archive did not hold the declared member. Every refusal is emitted
            to the audit sink before it is raised, and none of them writes a file.
    """
    host = host_platform()
    build = pin.for_host(host)
    if build is None:
        _refuse(
            pin,
            caller_did=caller_did,
            sink=audit_sink,
            tier=tier,
            reason="unpinned_platform",
            expected=f"a digest pinned for {host}",
            actual=f"pinned only for {', '.join(sorted(pin.platforms))}",
            message=(
                f"{pin.package} publishes no build Arc has a digest for on {host} — "
                f"install it by hand, or add that platform's published digest to the bundle"
            ),
        )
    # The basename, not the declared path: it is both the name the binary is
    # installed under and the whole traversal defence, so it is resolved once,
    # here, where an empty one is still a refusal that has written nothing.
    name = Path(build.member).name
    if not name:
        _refuse(
            pin,
            caller_did=caller_did,
            sink=audit_sink,
            tier=tier,
            reason="not_a_binary",
            expected="an archive holding one executable",
            actual="a package its own package manager installs",
            message=(
                f"{pin.package} is pinned but is not a single binary Arc can place on "
                f"PATH — run the bundle's install steps instead"
            ),
        )

    payload = await _download(pin, build, fetch or https_get, caller_did, audit_sink, tier)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != build.sha256:
        _refuse(
            pin,
            caller_did=caller_did,
            sink=audit_sink,
            tier=tier,
            reason="hash_mismatch",
            expected=build.sha256,
            actual=digest,
            message=(
                f"{pin.package} downloaded from {build.url} is not the approved build — "
                f"nothing was installed"
            ),
        )

    body = _member_bytes(pin, build, payload, caller_did, audit_sink, tier)
    path = _place(body, install_dir, name)
    emit(
        AuditEvent(
            actor_did=caller_did,
            action=_ACTION,
            target=f"artifact:{pin.package}@{pin.version}",
            outcome="allow",
            tier=tier.value,
            extra={
                "package": pin.package,
                "platform": host_platform(),
                "sha256": digest,
                "path": str(path),
            },
        ),
        audit_sink,
    )
    return path


async def _download(
    pin: ArtifactPin,
    build: PlatformArtifact,
    fetch: Fetcher,
    caller_did: str,
    sink: AuditSink,
    tier: Tier,
) -> bytes:
    """Fetch the pinned bytes, reporting a failure as a refusal rather than a traceback."""
    try:
        return await fetch(build.url)
    except Exception as exc:  # reason: any transport failure is one refusal to the operator
        _refuse(
            pin,
            caller_did=caller_did,
            sink=sink,
            tier=tier,
            reason="download_failed",
            expected=build.url,
            actual=f"{type(exc).__name__}: {exc}",
            message=f"could not download {pin.package} from {build.url} — {exc}",
        )


def _member_bytes(
    pin: ArtifactPin,
    build: PlatformArtifact,
    payload: bytes,
    caller_did: str,
    sink: AuditSink,
    tier: Tier,
) -> bytes:
    """Read the one declared member out of already-verified bytes.

    Never ``extractall``: only the single named member is read, so no other entry
    in the archive can put a file anywhere.

    Not every release is an archive. Some vendors publish the executable itself, and
    bytes that verified against their pinned digest and then failed to untar are a
    build correctly pinned and impossible to install. So the URL's own suffix
    decides: an archive is opened, and anything else already IS the binary.
    """
    if not build.url.endswith(_ARCHIVE_SUFFIXES):
        return payload
    read = _zip_member if build.url.endswith(".zip") else _tar_member
    try:
        return read(payload, build.member)
    except KeyError:
        _refuse(
            pin,
            caller_did=caller_did,
            sink=sink,
            tier=tier,
            reason="member_missing",
            expected=build.member,
            actual="the archive does not hold it",
            message=(
                f"{pin.package} downloaded and verified, but holds no {build.member} — "
                f"the bundle names the wrong path inside the archive"
            ),
        )
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
        _refuse(
            pin,
            caller_did=caller_did,
            sink=sink,
            tier=tier,
            reason="unreadable_archive",
            expected="a tar.gz or zip archive",
            actual=f"{type(exc).__name__}: {exc}",
            message=f"{pin.package} verified but could not be unpacked — {exc}",
        )


def _tar_member(payload: bytes, member: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        stream = archive.extractfile(member)
        if stream is None:
            raise KeyError(member)
        return stream.read()


def _zip_member(payload: bytes, member: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return archive.read(member)


def _place(body: bytes, install_dir: Path, name: str) -> Path:
    """Write the binary under its BASENAME, which is the whole traversal defence.

    ``Path("../../etc/passwd").name`` is ``passwd``: there is no member a manifest
    can declare that resolves outside ``install_dir``.
    """
    install_dir.mkdir(parents=True, exist_ok=True)
    path = install_dir / name
    path.write_bytes(body)
    path.chmod(_BINARY_MODE)
    return path


def _refuse(
    pin: ArtifactPin,
    *,
    caller_did: str,
    sink: AuditSink,
    tier: Tier,
    reason: str,
    expected: str,
    actual: str,
    message: str,
) -> NoReturn:
    """Record the refusal, then raise it — in that order, so neither can be lost."""
    emit(
        AuditEvent(
            actor_did=caller_did,
            action=_ACTION,
            target=f"artifact:{pin.package}@{pin.version}",
            outcome="deny",
            tier=tier.value,
            extra={
                "package": pin.package,
                "platform": host_platform(),
                "reason": reason,
                "expected": expected,
                "actual": actual,
            },
        ),
        sink,
    )
    raise ExtensionError(
        code=_REFUSED,
        message=message,
        details={"package": pin.package, "reason": reason, "expected": expected, "actual": actual},
    )


async def https_get(url: str) -> bytes:
    """The shipped fetcher: one bounded HTTPS GET, following the release redirect.

    Imported inside the function so the module's import list stays free of
    anything that could execute what it installs — the structural property
    ``test_host_install`` asserts.
    """
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=_FETCH_TIMEOUT_SECONDS) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks += chunk
                if len(chunks) > _MAX_ARTIFACT_BYTES:
                    raise ExtensionError(
                        code=_REFUSED,
                        message=f"{url} is larger than the {_MAX_ARTIFACT_BYTES} byte ceiling",
                        details={"url": url},
                    )
            return bytes(chunks)


__all__ = ["Fetcher", "host_install_dir", "https_get", "install_pinned_binary"]
