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

An npm tarball (``member`` empty, ``.tgz``) is the one build that is not a single
executable. It is installed into an Arc-owned prefix under the operator root —
never ``npm -g``, never a system path — after its sha256 is checked and its
entries are scanned for traversal, with ``--ignore-scripts`` so no package
lifecycle script ever runs. The argv is fixed in this module; no manifest string
reaches it. Every install is RECORDED (:func:`recorded_install_path`) so a
connector finds the program by the path Arc put it at, not by whatever happens to
be on ``PATH``.

A deployment at federal stringency never installs from the network unless the
artifact's digest is on the operator's signed allowlist.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import zipfile
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.paths import operator_root

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

#: How the fixed ``npm install`` argv is run: argv in, (exit code, output) out.
#: Injected so a test never needs Node or a registry.
NpmRunner = Callable[[Sequence[str]], Awaitable[tuple[int, str]]]

#: How a program name becomes a path on PATH. Injected for the same reason.
Which = Callable[[str], str | None]

#: ``npm install`` is the slowest step of the one install that has any.
_NPM_TIMEOUT_SECONDS = 300.0

#: How much of npm's own output a refusal carries back to the operator.
_NPM_TAIL_CHARS = 300

#: The one place an npm package's files land, in a name no manifest can steer.
_UNSAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")

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


def host_tools_dir() -> Path:
    """The Arc-owned prefix for host programs: ``<operator root>/host-tools``.

    Resolved per call. Holds the npm prefixes and the install record, so an
    operator who deletes it removes exactly what Arc installed and nothing else.
    """
    return operator_root() / "host-tools"


def _record_file() -> Path:
    return host_tools_dir() / "installed.json"


def recorded_install_path(name: str) -> Path | None:
    """Where Arc put ``name``, or ``None`` when it never installed it.

    The answer is the executable's own path, not a directory to search, so a
    connector runs exactly the program whose digest was checked. A recorded path
    whose file is gone or no longer executable is not an answer: the install was
    removed behind Arc's back and the prerequisite is missing again.
    """
    try:
        recorded = json.loads(_record_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = recorded.get(name) if isinstance(recorded, dict) else None
    if not isinstance(raw, str):
        return None
    path = Path(raw)
    return path if path.is_file() and os.access(path, os.X_OK) else None


def _record_install(name: str, path: Path) -> None:
    """Merge one ``name -> path`` entry into the record, atomically."""
    record = _record_file()
    record.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current[name] = str(path)
    staged = record.with_suffix(".json.tmp")
    staged.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    staged.replace(record)


async def install_pinned_binary(
    pin: ArtifactPin,
    *,
    install_dir: Path,
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    fetch: Fetcher | None = None,
    npm_run: NpmRunner | None = None,
    which: Which = shutil.which,
    npm_prefix: Path | None = None,
    federal_allowlist: Collection[str] = (),
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
        npm_run: How the fixed ``npm install`` argv runs. Defaults to a bounded
            subprocess; only an npm tarball ever uses it.
        which: Presence lookup for ``node`` and ``npm``.
        npm_prefix: Where an npm package is installed. Defaults to a folder under
            :func:`host_tools_dir`.
        federal_allowlist: sha256 digests the operator allowlisted. At federal
            stringency a build whose digest is not here is not fetched at all.

    Returns:
        The path of the installed executable.

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
    if tier is Tier.FEDERAL and build.sha256 not in federal_allowlist:
        _refuse(
            pin,
            caller_did=caller_did,
            sink=audit_sink,
            tier=tier,
            reason="federal_not_allowlisted",
            expected="a digest on the signed allowlist",
            actual=build.sha256,
            message=(
                f"{pin.package} is not on this deployment's approved list, so Arc will not "
                f"download it. Ask your administrator to approve it."
            ),
        )
    if not build.member and _is_npm_tarball(build.url):
        return await _install_npm_package(
            pin,
            build,
            install_dir=install_dir,
            caller_did=caller_did,
            audit_sink=audit_sink,
            tier=tier,
            fetch=fetch or https_get,
            npm_run=npm_run or run_npm,
            which=which,
            npm_prefix=npm_prefix,
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
    _record_install(name, path)
    _emit_installed(pin, digest, path, caller_did, audit_sink, tier)
    return path


def _emit_installed(
    pin: ArtifactPin, digest: str, path: Path, caller_did: str, sink: AuditSink, tier: Tier
) -> None:
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
        sink,
    )


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


def _is_npm_tarball(url: str) -> bool:
    """An npm registry tarball is named ``<pkg>-<version>.tgz``; nothing else is one."""
    return url.endswith(".tgz")


def _package_slug(pin: ArtifactPin) -> str:
    """A folder name for one package version, with nothing in it a path could use."""
    return _UNSAFE_SLUG.sub("-", f"{pin.package.lstrip('@')}-{pin.version}").strip("-.")


@dataclass(frozen=True)
class _Verdicts:
    """Who is refusing what, so each npm-path check is one line instead of seven."""

    pin: ArtifactPin
    caller_did: str
    sink: AuditSink
    tier: Tier

    def refuse(self, reason: str, expected: str, actual: str, message: str) -> NoReturn:
        _refuse(
            self.pin,
            caller_did=self.caller_did,
            sink=self.sink,
            tier=self.tier,
            reason=reason,
            expected=expected,
            actual=actual,
            message=message,
        )


async def _install_npm_package(
    pin: ArtifactPin,
    build: PlatformArtifact,
    *,
    install_dir: Path,
    caller_did: str,
    audit_sink: AuditSink,
    tier: Tier,
    fetch: Fetcher,
    npm_run: NpmRunner,
    which: Which,
    npm_prefix: Path | None,
) -> Path:
    """Install a verified npm tarball into an Arc-owned prefix, scripts disabled.

    Order matters and is the whole defence: Node is checked before a byte is
    fetched, the digest before the archive is read, the archive's entries before
    npm sees it, and a failed npm run removes the prefix it created so a refusal
    leaves nothing behind.
    """
    verdicts = _Verdicts(pin, caller_did, audit_sink, tier)
    if which("node") is None or which("npm") is None:
        verdicts.refuse(
            "node_missing",
            "Node.js 20 or newer on this computer",
            "node or npm not found",
            f"{pin.package} needs Node.js, and this computer does not have it. "
            "Install Node.js 20 or newer from nodejs.org, then press Install again.",
        )
    payload = await _download(pin, build, fetch, caller_did, audit_sink, tier)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != build.sha256:
        verdicts.refuse(
            "hash_mismatch",
            build.sha256,
            digest,
            f"{pin.package} downloaded from {build.url} is not the approved build — "
            "nothing was installed",
        )
    bin_name = _scan_npm_tarball(payload, verdicts)

    prefix = (npm_prefix or host_tools_dir() / "npm") / _package_slug(pin)
    created = not prefix.exists()
    staged = prefix / ".stage" / "package.tgz"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(payload)
    staged.chmod(0o600)
    exit_code, output = await npm_run(_npm_argv(prefix, staged))
    shim = prefix / "node_modules" / ".bin" / bin_name
    if exit_code != 0 or not shim.exists():
        if created:
            shutil.rmtree(prefix, ignore_errors=True)
        verdicts.refuse(
            "npm_failed",
            "npm installs the verified package",
            f"exit {exit_code}: {output[-_NPM_TAIL_CHARS:]}",
            f"{pin.package} could not be installed — npm reported a problem",
        )
    shutil.rmtree(prefix / ".stage", ignore_errors=True)

    link = _link_into(install_dir, bin_name, shim)
    _record_install(bin_name, shim)
    _emit_installed(pin, digest, shim, caller_did, audit_sink, tier)
    return link


def _npm_argv(prefix: Path, tarball: Path) -> list[str]:
    """The one npm command Arc ever runs. Fixed here; no manifest string is in it.

    ``--ignore-scripts`` is the control: a package's install, postinstall and
    prepare hooks are arbitrary code, and the digest only proves WHICH bytes were
    approved, not that running them is safe.
    """
    return [
        "install",
        "--prefix",
        str(prefix),
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--no-save",
        "--no-package-lock",
        str(tarball),
    ]


def _scan_npm_tarball(payload: bytes, verdicts: _Verdicts) -> str:
    """Refuse an unsafe archive, and return the program name the package declares.

    npm extracts the tarball itself, so Arc cannot rely on its own basename rule
    here. Every entry is checked first: an absolute path, a ``..`` segment, a link
    that points out of the package, or a device node is a refusal.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            members = archive.getmembers()
            for member in members:
                _check_entry(member, verdicts)
            return _declared_bin(archive, members, verdicts)
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        verdicts.refuse(
            "unreadable_archive",
            "an npm tarball",
            f"{type(exc).__name__}: {exc}",
            f"the download verified but could not be read as an npm package — {exc}",
        )


def _escapes(name: str) -> bool:
    path = PurePosixPath(name)
    return path.is_absolute() or ".." in path.parts


def _check_entry(member: tarfile.TarInfo, verdicts: _Verdicts) -> None:
    unsafe = _escapes(member.name) or member.isdev()
    if member.islnk():
        unsafe = unsafe or _escapes(os.path.normpath(member.linkname))
    elif member.issym():
        beside = PurePosixPath(member.name).parent / member.linkname
        unsafe = unsafe or PurePosixPath(member.linkname).is_absolute()
        unsafe = unsafe or _escapes(os.path.normpath(str(beside)))
    if unsafe:
        verdicts.refuse(
            "path_traversal",
            "entries inside the package folder",
            member.name,
            "the downloaded package holds a file that points outside its own folder — "
            "nothing was installed",
        )


def _declared_bin(
    archive: tarfile.TarFile, members: list[tarfile.TarInfo], verdicts: _Verdicts
) -> str:
    manifest = next(
        (m for m in members if re.fullmatch(r"[^/]+/package\.json", m.name) and m.isfile()),
        None,
    )
    stream = archive.extractfile(manifest) if manifest else None
    declared = json.loads(stream.read()).get("bin") if stream else None
    name = ""
    if isinstance(declared, dict) and declared:
        name = str(next(iter(declared)))
    elif isinstance(declared, str):
        name = Path(declared).name
    if not name or Path(name).name != name or name.startswith("."):
        verdicts.refuse(
            "not_a_binary",
            "an npm package declaring one program",
            "no usable bin entry in package.json",
            f"{verdicts.pin.package} declares no program Arc can place",
        )
    return name


def _link_into(install_dir: Path, name: str, target: Path) -> Path:
    """Point ``install_dir/name`` at the Arc-owned shim, replacing a stale one."""
    install_dir.mkdir(parents=True, exist_ok=True)
    link = install_dir / name
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return link


async def run_npm(argv: Sequence[str]) -> tuple[int, str]:
    """The shipped npm runner: one bounded, scrubbed ``npm`` run with scripts off.

    Async subprocess imported here so the module's import list stays free of
    anything that could execute a manifest string; ``argv`` comes from
    :func:`_npm_argv` alone. The environment is rebuilt rather than inherited so
    registry tokens in the operator's shell never reach a package install.
    """
    import asyncio

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "npm_config_ignore_scripts": "true",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
    }
    process = await asyncio.create_subprocess_exec(
        "npm",
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), _NPM_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 1, "npm did not finish in time"
    return process.returncode or 0, output.decode("utf-8", "replace")


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


__all__ = [
    "Fetcher",
    "NpmRunner",
    "host_install_dir",
    "host_tools_dir",
    "https_get",
    "install_pinned_binary",
    "recorded_install_path",
    "run_npm",
]
