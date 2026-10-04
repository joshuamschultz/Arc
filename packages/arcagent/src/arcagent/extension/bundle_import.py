"""UJ-6 — a connector package travels from an operator's upload to a signed, installed bundle.

The CLI path (``arc connector sign`` then ``install-bundle``) assumes someone has a terminal
and a folder. This module is the same path for a person with only a browser and an archive,
and it adds nothing to what the loader trusts: the end state is exactly the bundle the CLI
produces — every file signed by the operator key through
:meth:`~arcagent.connections.Connections.sign_bundle`, installed by
:meth:`~arcagent.connections.Connections.install_bundle` into ``~/.arc/extensions``, and
re-verified by the loader on every load.

What sits in front of that, and why:

* **Unpack without trusting the archive.** No ``extractall``. Every entry is read into
  memory through a budget: no absolute or ``..`` path, no symlink or hard link, no device
  file, at most :data:`MAX_ENTRIES` entries, at most :data:`MAX_EXPANDED_BYTES` unpacked
  and no entry that inflates more than :data:`MAX_RATIO` times. Nothing in the package is
  imported, spawned or parsed beyond its manifest and a static AST read of its Python.
* **Review what will run.** The manifest is parsed by the loader's own parser at the
  deployment tier, and the loader's egress refusal runs on it. The tools it declares must
  match what its code offers (statically: a native bundle's code must name each declared
  tool, a CLI bundle's commands must be exactly its tools, and no top-level ``@tool``
  may be undeclared). The review lists every tool, secret, host program, network host,
  skill and file, and flags what executes code or reaches a network.
* **Sign what was reviewed, nothing else (TOCTOU).** The digest of the reviewed bytes is
  held in this process, not beside the staged files an attacker could rewrite. Approval
  re-reads the staging, refuses on any difference, writes the bytes into a fresh
  directory, signs that, and checks the digest again before installing.
* **Publisher verdict.** A package whose signatures verify under the operator key or an
  ``issuers.toml`` entry is a verified publisher. Anything else needs the operator to type
  the package name. At federal tier only a verified publisher is accepted.

Every verdict is audited through :func:`arctrust.audit.emit`.
"""

from __future__ import annotations

import ast
import hashlib
import os
import secrets
import shutil
import stat
import tarfile
import tempfile
import time
import tomllib
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any, Literal, NoReturn
from urllib.parse import urlsplit

from arctrust.artifact import ArtifactSignature, verify_artifact
from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.canonical import canonical_json
from arctrust.paths import installed_extensions_dir, trust_dir
from arctrust.trust_store import TrustStoreError, load_issuer_pubkey

from arcagent.capabilities.artifact_signing import SIDECAR_SUFFIX
from arcagent.capabilities.isolated_tool import IsolatedCapabilityError, parse_authored_tools
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.catalog import (
    MANIFEST_NAME,
    in_operator_tree,
    is_config_only_bundle,
    resolve_extension_roots,
    validate_extension_name,
)
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.tools._egress_policy import first_forbidden_egress, is_egress

if TYPE_CHECKING:
    from arcagent.connections import Connections

#: The largest archive an operator may upload.
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
#: The most entries (files and folders) an archive may hold.
MAX_ENTRIES = 2000
#: The most bytes an archive may unpack to.
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
#: The most one entry, or a whole tarball, may inflate over its compressed size.
MAX_RATIO = 100
#: Below this unpacked size the ratio is not judged (tiny text compresses very well).
_RATIO_FLOOR = 1024 * 1024
_MAX_DEPTH = 16
_MAX_PATH = 240
#: How long a staged package waits for the operator's decision.
STAGING_TTL_SECONDS = 3600.0
_STAGING_DIRNAME = ".staging"
_ISSUERS_FILE = "issuers.toml"
_CHUNK = 64 * 1024
_JUNK_DIRS = frozenset({"__MACOSX"})
_JUNK_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
#: File kinds that run as code if anything executes them.
_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".py", ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".sh", ".bash", ".zsh",
        ".js", ".mjs", ".cjs", ".rb", ".pl", ".ps1", ".bat", ".cmd", ".jar", ".wasm", ".bin",
    }
)  # fmt: skip

#: The refusal code every failure here raises; ``details["reason"]`` says which.
BUNDLE_REFUSED = "BUNDLE_REFUSED"

PublisherStatus = Literal["verified", "unknown", "unsigned"]


# --- what a review says ---------------------------------------------------------------


@dataclass(frozen=True)
class BundleFile:
    """One file the package ships, with whether it is a kind that runs as code."""

    path: str
    size: int
    executes: bool


@dataclass(frozen=True)
class ToolView:
    """One declared tool as the operator reviews it."""

    name: str
    description: str
    classification: str
    capability_tags: tuple[str, ...]
    network: bool


@dataclass(frozen=True)
class SecretView:
    """One value the package asks the operator for. Never a value, only its name."""

    name: str
    prompt: str
    sensitive: bool
    required: bool


@dataclass(frozen=True)
class Publisher:
    """Who signed the uploaded package, and whether this deployment trusts them."""

    status: PublisherStatus
    signer_did: str = ""


@dataclass(frozen=True)
class BundleDiff:
    """What an update changes against the installed version."""

    installed_version: str
    tools_added: tuple[str, ...]
    tools_removed: tuple[str, ...]
    tools_changed: tuple[str, ...]
    new_secrets: tuple[str, ...]
    new_egress: tuple[str, ...]


@dataclass(frozen=True)
class BundleReview:
    """Everything the operator approves, computed from the staged bytes."""

    name: str
    display_name: str
    version: str
    description: str
    attachment: str
    tier_floor: str
    publisher: Publisher
    tools: tuple[ToolView, ...]
    secrets: tuple[SecretView, ...]
    host_programs: tuple[str, ...]
    egress_hosts: tuple[str, ...]
    skills: tuple[str, ...]
    files: tuple[BundleFile, ...]
    executes_code: bool
    needs_network: bool
    flags: tuple[str, ...]
    digest: str
    update: BundleDiff | None
    confirm_required: bool


@dataclass(frozen=True)
class StagedBundle:
    """A package waiting for approval. ``expires_at`` is seconds on the staging clock."""

    staging_id: str
    review: BundleReview
    expires_at: float


@dataclass(frozen=True)
class InstalledBundle:
    """One operator-installed bundle and the connections that use it."""

    name: str
    display_name: str
    version: str
    signer_did: str
    used_by: tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class _Entry:
    path: PurePosixPath
    data: bytes

    @property
    def is_sidecar(self) -> bool:
        return self.path.name.endswith(SIDECAR_SUFFIX)


@dataclass
class _Record:
    review: BundleReview
    directory: Path
    expires_at: float


def _refuse(reason: str, message: str, **details: Any) -> ExtensionError:
    return ExtensionError(
        code=BUNDLE_REFUSED, message=message, details={"reason": reason, **details}
    )


# --- staging --------------------------------------------------------------------------


class BundleStaging:
    """Holds packages between upload and approval, with the digest each review covered.

    The digests live in this object, never on disk beside the staged files: anyone able
    to rewrite a staged file could rewrite a digest stored next to it. Losing them on a
    restart only means the operator uploads again.

    Args:
        clock: Seconds, monotonic. Injected so a test can expire a staging.
        ttl: How long a staged package waits for a decision.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = STAGING_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._records: dict[str, _Record] = {}

    def stage_archive(
        self, archive: Path, *, connections: Connections, audit_sink: AuditSink
    ) -> StagedBundle:
        """Unpack an uploaded ``.zip`` / ``.tar.gz`` into staging and review it.

        Raises:
            ExtensionError: ``BUNDLE_REFUSED``; ``details["reason"]`` names why.
        """
        return self._stage(lambda: read_archive(Path(archive)), connections, audit_sink)

    def stage_local(
        self, name: str, *, connections: Connections, audit_sink: AuditSink
    ) -> StagedBundle:
        """Stage a code-bearing bundle sitting in the operator tree, so it can be signed.

        Nothing executes from ``~/arc/extensions``; this is the path that reviews such a
        bundle and, on approval, installs a signed copy where it may run.
        """

        def read() -> list[_Entry]:
            try:
                validate_extension_name(name)
            except ValueError as exc:
                raise _refuse("invalid_name", str(exc)) from exc
            folder = Path(connections.world.arc_dir) / "extensions" / name
            if (
                folder.is_symlink()
                or not folder.is_dir()
                or not in_operator_tree(folder)
                or is_config_only_bundle(folder, connections.world.tier)
            ):
                raise _refuse("not_found", f"there is no unsigned package named {name!r}")
            return read_tree(folder)

        return self._stage(read, connections, audit_sink)

    def approve(
        self,
        staging_id: str,
        *,
        confirm_name: str,
        connections: Connections,
        audit_sink: AuditSink,
    ) -> InstalledBundle:
        """Sign exactly the reviewed bytes with the operator key and install them.

        Raises:
            ExtensionError: ``BUNDLE_REFUSED`` (expired, confirm_mismatch,
                changed_after_review, a tier refusal) or a refusal from
                :meth:`Connections.install_bundle`. Each is audited.
        """
        record = self._live(staging_id)
        review = record.review
        try:
            installed = self._install(record, confirm_name, connections)
        except ExtensionError as exc:
            _audit(audit_sink, "connector.bundle_approved", review.name, "deny", exc, connections)
            if exc.details.get("reason") == "changed_after_review":
                self.discard(staging_id)
            raise
        self.discard(staging_id)
        _audit(
            audit_sink,
            "connector.bundle_approved",
            review.name,
            "allow",
            None,
            connections,
            digest=review.digest,
            publisher=review.publisher.signer_did or review.publisher.status,
            update="yes" if review.update is not None else "no",
        )
        return installed

    @property
    def max_archive_bytes(self) -> int:
        """The largest archive :meth:`stage_archive` accepts, for a surface to cap uploads."""
        return MAX_ARCHIVE_BYTES

    def now(self) -> float:
        """The staging clock, which :attr:`StagedBundle.expires_at` is measured on."""
        return self._clock()

    def discard(self, staging_id: str) -> bool:
        """Delete one staged package. Returns whether there was one to delete."""
        record = self._records.pop(staging_id, None)
        if record is None:
            return False
        shutil.rmtree(record.directory.parent, ignore_errors=True)
        return True

    def get(self, staging_id: str) -> StagedBundle | None:
        """The staged package, if it exists and has not expired."""
        self._sweep()
        record = self._records.get(staging_id)
        if record is None:
            return None
        return StagedBundle(staging_id, record.review, record.expires_at)

    # --- internals ---------------------------------------------------------------

    def _stage(
        self,
        read: Callable[[], list[_Entry]],
        connections: Connections,
        audit_sink: AuditSink,
    ) -> StagedBundle:
        self._sweep()
        name = ""
        try:
            entries = read()
            review = review_entries(entries, connections=connections)
            name = review.name
            directory = _write_staging(review.name, entries)
        except ExtensionError as exc:
            _audit(audit_sink, "connector.bundle_staged", name or "?", "deny", exc, connections)
            raise
        staging_id = directory.parent.name
        expires_at = self._clock() + self._ttl
        self._records[staging_id] = _Record(
            review=review, directory=directory, expires_at=expires_at
        )
        _audit(
            audit_sink,
            "connector.bundle_staged",
            review.name,
            "allow",
            None,
            connections,
            digest=review.digest,
            publisher=review.publisher.status,
        )
        return StagedBundle(staging_id, review, expires_at)

    def _live(self, staging_id: str) -> _Record:
        self._sweep()
        record = self._records.get(staging_id)
        if record is None:
            raise _refuse(
                "expired", "that package is no longer waiting for review; upload it again"
            )
        return record

    def _sweep(self) -> None:
        now = self._clock()
        for staging_id in [k for k, r in self._records.items() if r.expires_at <= now]:
            self.discard(staging_id)

    def _install(
        self, record: _Record, confirm_name: str, connections: Connections
    ) -> InstalledBundle:
        review = record.review
        if review.confirm_required and confirm_name.strip() != review.name:
            raise _refuse(
                "confirm_mismatch", f"type the package name {review.name!r} to approve it"
            )
        _require_allowed_publisher(review.publisher, connections)
        shipped = _shipped(read_tree(record.directory))
        _require_digest(shipped, review.digest)
        root = _staging_root()
        with tempfile.TemporaryDirectory(dir=root, prefix=".sign-") as scratch:
            folder = Path(scratch) / review.name
            _write_files(folder, shipped)
            connections.sign_bundle(folder)
            _require_digest(_shipped(read_tree(folder)), review.digest)
            target = connections.install_bundle(folder, replace=review.update is not None)
        return _installed(target, connections)


def _staging_root() -> Path:
    """``~/.arc/extensions/.staging``: Arc-owned, private, and never a bundle name."""
    root = installed_extensions_dir() / _STAGING_DIRNAME
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _write_staging(name: str, entries: list[_Entry]) -> Path:
    holder = _staging_root() / secrets.token_hex(16)
    holder.mkdir(mode=0o700)
    folder = holder / name
    try:
        _write_files(folder, entries)
    except OSError as exc:
        shutil.rmtree(holder, ignore_errors=True)
        raise _refuse("unwritable", f"could not stage the package: {exc.strerror}") from exc
    return folder


def _write_files(folder: Path, entries: Iterable[_Entry]) -> None:
    folder.mkdir(mode=0o700, parents=True)
    for entry in entries:
        target = folder.joinpath(*entry.path.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(target, "xb") as handle:  # never through an existing file or link
            handle.write(entry.data)
        target.chmod(0o644)


# --- reading an archive or a folder ---------------------------------------------------


class _Budget:
    """Counts what an archive unpacks to and refuses the moment it is too much."""

    def __init__(self) -> None:
        self.expanded = 0
        self.entries = 0

    def count_entry(self) -> None:
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            raise _refuse("too_many_entries", f"the package holds more than {MAX_ENTRIES} entries")

    def read(self, handle: IO[bytes], declared: int, compressed: int | None) -> bytes:
        if compressed is not None and declared > _RATIO_FLOOR:
            if declared > MAX_RATIO * max(compressed, 1):
                raise _refuse("archive_bomb", "a file in the package inflates far beyond its size")
        if self.expanded + declared > MAX_EXPANDED_BYTES:
            raise _refuse("too_large", "the package unpacks to more than Arc accepts")
        chunks: list[bytes] = []
        total = 0
        while chunk := handle.read(_CHUNK):
            total += len(chunk)
            if total > declared or self.expanded + total > MAX_EXPANDED_BYTES:
                raise _refuse("too_large", "the package unpacks to more than it declares")
            chunks.append(chunk)
        self.expanded += total
        return b"".join(chunks)


def read_archive(archive: Path) -> list[_Entry]:
    """Read a ``.zip`` or gzipped tarball into memory, refusing anything unsafe.

    Raises:
        ExtensionError: ``BUNDLE_REFUSED`` with the reason.
    """
    try:
        size = archive.stat().st_size
        with archive.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise _refuse("not_an_archive", "the upload could not be read") from exc
    if size > MAX_ARCHIVE_BYTES:
        raise _refuse("too_large", f"the package is over {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB")
    try:
        if magic.startswith(b"PK\x03\x04"):
            entries = _zip_entries(archive)
        elif magic.startswith(b"\x1f\x8b"):
            entries = _tar_entries(archive, size)
        else:
            raise _refuse("not_an_archive", "choose a .zip or .tar.gz package")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, NotImplementedError) as exc:
        raise _refuse("not_an_archive", "the package archive is damaged") from exc
    return _normalize(entries)


def _zip_entries(archive: Path) -> list[_Entry]:
    budget = _Budget()
    seen: set[str] = set()
    entries: list[_Entry] = []
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if len(infos) > MAX_ENTRIES:
            raise _refuse("too_many_entries", f"the package holds more than {MAX_ENTRIES} entries")
        for info in infos:
            budget.count_entry()
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.flag_bits & 0x1:
                raise _refuse("encrypted", "the package holds an encrypted file")
            if kind == stat.S_IFLNK:
                raise _refuse("symlink", f"the package holds a link: {info.filename}")
            if info.is_dir():
                _safe_path(info.filename, seen, directory=True)
                continue
            if kind not in (0, stat.S_IFREG):
                raise _refuse("special_file", f"the package holds a special file: {info.filename}")
            path = _safe_path(info.filename, seen)
            with bundle.open(info) as handle:
                data = budget.read(handle, info.file_size, info.compress_size)
            entries.append(_Entry(path, data))
    return entries


def _tar_entries(archive: Path, size: int) -> list[_Entry]:
    budget = _Budget()
    seen: set[str] = set()
    entries: list[_Entry] = []
    with tarfile.open(archive, mode="r:gz") as bundle:
        for member in bundle:
            budget.count_entry()
            if member.issym() or member.islnk():
                raise _refuse("symlink", f"the package holds a link: {member.name}")
            if member.isdir():
                _safe_path(member.name, seen, directory=True)
                continue
            if not member.isreg():
                raise _refuse("special_file", f"the package holds a special file: {member.name}")
            path = _safe_path(member.name, seen)
            handle = bundle.extractfile(member)
            if handle is None:
                raise _refuse(
                    "special_file", f"the package holds an unreadable entry: {member.name}"
                )
            entries.append(_Entry(path, budget.read(handle, member.size, None)))
    if budget.expanded > _RATIO_FLOOR and budget.expanded > MAX_RATIO * max(size, 1):
        raise _refuse("archive_bomb", "the package inflates far beyond its size")
    return entries


def read_tree(folder: Path) -> list[_Entry]:
    """Read a bundle folder into memory under the same rules an archive is held to."""
    budget = _Budget()
    seen: set[str] = set()
    entries: list[_Entry] = []
    pending = [Path(folder)]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                budget.count_entry()
                path = Path(child.path)
                relative = path.relative_to(folder).as_posix()
                status = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(status.st_mode):
                    raise _refuse("symlink", f"the package holds a link: {relative}")
                if stat.S_ISDIR(status.st_mode):
                    _safe_path(relative, seen, directory=True)
                    pending.append(path)
                    continue
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise _refuse("special_file", f"the package holds a special file: {relative}")
                safe = _safe_path(relative, seen)
                with open(path, "rb") as handle:
                    entries.append(_Entry(safe, budget.read(handle, status.st_size, None)))
    return _normalize(entries)


def _safe_path(raw: str, seen: set[str], *, directory: bool = False) -> PurePosixPath:
    text = raw.rstrip("/") if directory else raw
    unsafe = (
        not text or "\x00" in text or "\\" in text or text.startswith("/") or len(text) > _MAX_PATH
    )
    path = PurePosixPath(text)
    if unsafe or any(part in ("", ".", "..") or ":" in part for part in path.parts):
        raise _refuse("unsafe_path", f"the package holds an unsafe path: {raw[:80]!r}")
    if len(path.parts) > _MAX_DEPTH:
        raise _refuse("unsafe_path", f"the package nests too deep: {raw[:80]!r}")
    if not directory:
        key = unicodedata.normalize("NFC", path.as_posix()).casefold()
        if key in seen:
            raise _refuse("unsafe_path", f"the package holds two files named {raw[:80]!r}")
        seen.add(key)
    return path


def _normalize(entries: list[_Entry]) -> list[_Entry]:
    """Drop archive-manager noise and one wrapper folder; require a root manifest."""
    kept = [
        e for e in entries if e.path.parts[0] not in _JUNK_DIRS and e.path.name not in _JUNK_FILES
    ]
    roots = {e.path.parts[0] for e in kept}
    has_manifest = any(e.path.parts == (MANIFEST_NAME,) for e in kept)
    if not has_manifest and len(roots) == 1 and all(len(e.path.parts) > 1 for e in kept):
        kept = [_Entry(PurePosixPath(*e.path.parts[1:]), e.data) for e in kept]
    if not any(e.path.parts == (MANIFEST_NAME,) for e in kept):
        raise _refuse("no_manifest", f"the package has no {MANIFEST_NAME} at its top level")
    return sorted(kept, key=lambda e: e.path.as_posix())


# --- review ---------------------------------------------------------------------------


def review_entries(entries: list[_Entry], *, connections: Connections) -> BundleReview:
    """Validate a package's manifest and code, and describe everything it would do.

    Raises:
        ExtensionError: ``BUNDLE_REFUSED`` when the manifest, the code or the publisher
            is refused at this deployment's tier.
    """
    world = connections.world
    shipped = _shipped(entries)
    files = {e.path.as_posix(): e.data for e in shipped}
    manifest, raw = _parse_manifest(files[MANIFEST_NAME], world.tier)
    _require_egress_allowed(manifest, world.tier, world.egress_allow)
    _require_tools_match(manifest, files)
    _require_name_free(manifest.extension.name, world.arc_dir)
    publisher = _publisher(shipped, entries, world.arc_dir)
    _require_allowed_publisher(publisher, connections)
    tools = tuple(_tool_view(tool) for tool in manifest.tools.declared)
    egress = _egress_hosts(raw)
    executes = manifest.extension.attachment != "mcp" or _mcp_spawns(manifest)
    network = bool(egress) or any(tool.network for tool in tools)
    update = _diff(manifest, egress, world.tier)
    return BundleReview(
        name=manifest.extension.name,
        display_name=manifest.extension.label,
        version=manifest.extension.version,
        description=manifest.extension.description,
        attachment=manifest.extension.attachment,
        tier_floor=manifest.extension.tier_floor.value,
        publisher=publisher,
        tools=tools,
        secrets=tuple(
            SecretView(s.name, s.prompt, s.sensitive, s.required) for s in manifest.secrets
        ),
        host_programs=tuple(sorted(h.name for h in manifest.host_requires)),
        egress_hosts=egress,
        skills=tuple(
            sorted(
                {
                    p.split("/")[1]
                    for p in files
                    if p.startswith("skills/") and p.endswith("/SKILL.md")
                }
            )
        ),
        files=tuple(
            BundleFile(e.path.as_posix(), len(e.data), _executes(e.path)) for e in shipped
        ),
        executes_code=executes,
        needs_network=network,
        flags=_flags(manifest, executes, network, egress),
        digest=_digest(shipped),
        update=update,
        confirm_required=publisher.status != "verified" or update is not None,
    )


def _parse_manifest(data: bytes, tier: Tier) -> tuple[ExtensionManifest, dict[str, Any]]:
    """The loader's own parser, at this deployment's tier."""
    try:
        text = data.decode("utf-8")
        raw = tomllib.loads(text)
        manifest = load_manifest(text, tier=tier)
        validate_extension_name(manifest.extension.name)
    except ExtensionError as exc:
        raise _refuse("invalid_manifest", exc.message) from exc
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise _refuse("invalid_manifest", f"{MANIFEST_NAME} is not valid: {exc}") from exc
    return manifest, raw


def _require_egress_allowed(
    manifest: ExtensionManifest, tier: Tier, egress_allow: tuple[str, ...]
) -> None:
    """The loader's egress refusal, run before the operator is asked anything."""
    refusal = first_forbidden_egress(
        ((tool.name, tool.capability_tags) for tool in manifest.tools.declared),
        tier=tier,
        from_extension=True,
        egress_allow=egress_allow,
    )
    if refusal is not None:
        raise _refuse("egress_forbidden", refusal.message)


def _require_tools_match(manifest: ExtensionManifest, files: Mapping[str, bytes]) -> None:
    """The tools the manifest declares are the tools the code offers, both ways."""
    declared = {tool.name for tool in manifest.tools.declared}
    problems: list[str] = []
    allow = manifest.tools.allow
    if not manifest.tools.is_unbounded and allow is not None:
        problems += [f"{n} is allowed but not declared" for n in sorted(set(allow) - declared)]
        problems += [f"{n} is declared but not allowed" for n in sorted(declared - set(allow))]
    authored = _authored_tools(manifest, files)
    problems += [f"{n} is in the code but not declared" for n in sorted(authored - declared)]
    attachment = manifest.extension.attachment
    if attachment == "cli":
        commands = _cli_tools(manifest)
        problems += [f"{n} has no command" for n in sorted(declared - commands)]
        problems += [f"{n} is a command but not declared" for n in sorted(commands - declared)]
    elif attachment == "native":
        named = _python_strings(files) | authored
        problems += [
            f"{n} is declared but the code never names it" for n in sorted(declared - named)
        ]
    if problems:
        raise _refuse(
            "tools_mismatch",
            "the package's tools do not match its code: " + "; ".join(problems),
        )


def _authored_tools(manifest: ExtensionManifest, files: Mapping[str, bytes]) -> set[str]:
    """``@tool`` functions in top-level ``.py`` files: the loader registers these."""
    config = manifest.config.get(manifest.extension.attachment, {})
    entry = config.get("entrypoint", "") if isinstance(config, dict) else ""
    skipped = f"{str(entry).split('.', 1)[0]}.py" if entry else ""
    names: set[str] = set()
    with tempfile.TemporaryDirectory() as scratch:
        for path, data in files.items():
            if "/" in path or not path.endswith(".py") or path == skipped:
                continue
            source = Path(scratch) / path
            source.write_bytes(data)
            try:
                names |= {t.metadata.name for t in parse_authored_tools(source)}
            except IsolatedCapabilityError as exc:
                raise _refuse("invalid_code", f"{path}: {exc}") from exc
    return names


def _python_strings(files: Mapping[str, bytes]) -> set[str]:
    """Every string literal in the package's Python. Parsed, never run."""
    found: set[str] = set()
    for path, data in files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(data.decode("utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise _refuse("invalid_code", f"{path} is not valid Python") from exc
        found |= {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
    return found


def _cli_tools(manifest: ExtensionManifest) -> set[str]:
    commands: Any = manifest.config.get("cli", {}).get("commands", [])
    return {
        str(command["tool"])
        for command in (commands if isinstance(commands, list) else [])
        if isinstance(command, dict) and "tool" in command
    }


def _mcp_spawns(manifest: ExtensionManifest) -> bool:
    config = manifest.config.get("mcp", {})
    return isinstance(config, dict) and config.get("transport") == "stdio"


def _require_name_free(name: str, arc_dir: Path) -> None:
    """A package may not take a name a bundle Arc ships already answers to."""
    installed = installed_extensions_dir().resolve()
    for root in resolve_extension_roots(arc_dir):
        held = root / name
        if root.resolve() == installed or in_operator_tree(held):
            continue
        if held.exists() or held.is_symlink():
            raise _refuse("name_taken", f"{name!r} is the name of a connector Arc already ships")


def _tool_view(tool: Any) -> ToolView:
    tags = tuple(tool.capability_tags)
    return ToolView(
        name=tool.name,
        description=tool.description,
        classification=tool.classification,
        capability_tags=tags,
        network=is_egress(tags) or "web" in tags,
    )


def _egress_hosts(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Every network host the manifest names: URLs anywhere, plus declared cloud hosts."""
    hosts: set[str] = set()

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str):
            if key in {"login_host", "api_host"} and "{" not in value:
                hosts.add(value.lower())
            elif value.startswith(("https://", "http://")):
                host = urlsplit(value).hostname or ""
                if host and "{" not in host:
                    hosts.add(host.lower())

    walk({k: v for k, v in raw.items() if k != "extension"})
    return tuple(sorted(hosts))


def _executes(path: PurePosixPath) -> bool:
    return path.suffix.lower() in _EXECUTABLE_SUFFIXES


def _flags(
    manifest: ExtensionManifest, executes: bool, network: bool, egress: tuple[str, ...]
) -> tuple[str, ...]:
    flags: list[str] = []
    if manifest.extension.attachment == "native":
        flags.append("This package runs its own code inside Arc.")
    if (
        manifest.host_requires
        or manifest.extension.attachment == "cli"
        or (executes and manifest.extension.attachment == "mcp")
    ):
        programs = ", ".join(h.name for h in manifest.host_requires) or "a program"
        flags.append(f"This package starts {programs} on this machine.")
    if network:
        reach = f": {', '.join(egress)}" if egress else ""
        flags.append(f"This package reaches the network{reach}.")
    return tuple(flags)


def _diff(manifest: ExtensionManifest, egress: tuple[str, ...], tier: Tier) -> BundleDiff | None:
    """Compare with the installed bundle of the same name, if there is one."""
    current = installed_extensions_dir() / manifest.extension.name / MANIFEST_NAME
    if not current.is_file() or current.is_symlink():
        return None
    try:
        text = current.read_text(encoding="utf-8")
        before = load_manifest(text, tier=tier)
        before_egress = set(_egress_hosts(tomllib.loads(text)))
    except (OSError, ValueError, ExtensionError, tomllib.TOMLDecodeError):
        before, before_egress = None, set()
    old = {t.name: t for t in before.tools.declared} if before is not None else {}
    new = {t.name: t for t in manifest.tools.declared}
    old_secrets = {s.name for s in before.secrets} if before is not None else set()
    return BundleDiff(
        installed_version=before.extension.version if before is not None else "",
        tools_added=tuple(sorted(new.keys() - old.keys())),
        tools_removed=tuple(sorted(old.keys() - new.keys())),
        tools_changed=tuple(
            sorted(
                n for n in new.keys() & old.keys() if new[n].model_dump() != old[n].model_dump()
            )
        ),
        new_secrets=tuple(sorted({s.name for s in manifest.secrets} - old_secrets)),
        new_egress=tuple(sorted(set(egress) - before_egress)),
    )


# --- publisher and digest -------------------------------------------------------------


def _shipped(entries: Iterable[_Entry]) -> list[_Entry]:
    return [e for e in entries if not e.is_sidecar]


def _digest(shipped: Iterable[_Entry]) -> str:
    """One hash over every shipped path and its bytes: what the operator approves."""
    listing = [
        {"path": e.path.as_posix(), "sha256": hashlib.sha256(e.data).hexdigest()}
        for e in sorted(shipped, key=lambda e: e.path.as_posix())
    ]
    return hashlib.sha256(canonical_json(listing)).hexdigest()


def _require_digest(shipped: list[_Entry], expected: str) -> None:
    if _digest(shipped) != expected:
        raise _refuse(
            "changed_after_review",
            "the package changed after it was reviewed; upload it again and review it again",
        )


def _publisher(shipped: list[_Entry], entries: list[_Entry], arc_dir: Path) -> Publisher:
    """Who signed the package. A partial or broken signature set is tampering."""
    sidecars = {e.path.as_posix(): e.data for e in entries if e.is_sidecar}
    if not sidecars:
        return Publisher("unsigned")
    expected = {f"{e.path.as_posix()}{SIDECAR_SUFFIX}" for e in shipped}
    if set(sidecars) != expected:
        _tampered("files were added or removed after the package was signed")
    signatures: list[tuple[_Entry, ArtifactSignature]] = []
    for entry in shipped:
        try:
            signature = ArtifactSignature.from_json(
                sidecars[f"{entry.path.as_posix()}{SIDECAR_SUFFIX}"].decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError):
            _tampered(f"the signature for {entry.path.as_posix()} is unreadable")
        if not verify_artifact(entry.data, signature):
            _tampered(f"{entry.path.as_posix()} was changed after it was signed")
        signatures.append((entry, signature))
    signers = {(s.signer_did, s.public_key) for _, s in signatures}
    if len(signers) != 1:
        return Publisher("unknown")
    signer_did, _ = next(iter(signers))
    key = _trusted_key(signer_did, arc_dir)
    trusted = key is not None and all(
        verify_artifact(entry.data, signature, trusted_public_key=key)
        for entry, signature in signatures
    )
    return Publisher("verified" if trusted else "unknown", signer_did)


def _tampered(message: str) -> NoReturn:
    raise _refuse("tampered", f"the package's signatures do not match its files: {message}")


def _trusted_key(signer_did: str, arc_dir: Path) -> bytes | None:
    """The key ``signer_did`` must have used: the operator's own, or an approved issuer's."""
    from arctrust import SignerError, operator_signer_for
    from arctrust.policy import OperatorApprovalAuthority

    try:
        operator = operator_signer_for(base=arc_dir)
        if str(OperatorApprovalAuthority(operator).did) == signer_did:
            return bytes(operator.public_key)
    except (OSError, SignerError):
        pass
    try:
        return load_issuer_pubkey(signer_did, trust_dir=trust_dir(arc_dir))
    except TrustStoreError:
        return None


def _require_allowed_publisher(publisher: Publisher, connections: Connections) -> None:
    """Federal tier takes only a package signed by an approved publisher."""
    world = connections.world
    if world.tier is not Tier.FEDERAL or publisher.status == "verified":
        return
    if not (trust_dir(world.arc_dir) / _ISSUERS_FILE).is_file():
        raise _refuse(
            "federal_no_allowlist",
            "this deployment only accepts connector packages from approved publishers, "
            "and no approved publishers are set up",
        )
    raise _refuse(
        "federal_unsigned",
        "this deployment only accepts connector packages signed by an approved publisher, "
        "and this one is not",
    )


# --- installed bundles ----------------------------------------------------------------


@dataclass(frozen=True)
class UnsignedBundle:
    """A code-bearing bundle in the operator tree, waiting to be reviewed and signed."""

    name: str
    reason: str


def unsigned_local_bundles(connections: Connections) -> tuple[UnsignedBundle, ...]:
    """Every bundle the catalog will not run until the operator signs it."""
    from arctrust.audit import NullSink

    from arcagent.extension.catalog import SIGN_BUNDLE_ACTION, ExtensionCatalog

    world = connections.world
    listing = ExtensionCatalog(
        roots=resolve_extension_roots(world.arc_dir), tier=world.tier, audit_sink=NullSink()
    )
    return tuple(
        UnsignedBundle(entry.name, entry.error)
        for entry in listing.available()
        if entry.action == SIGN_BUNDLE_ACTION
    )


def installed_bundles(connections: Connections) -> tuple[InstalledBundle, ...]:
    """Every operator-installed bundle, with the connections that use it."""
    root = installed_extensions_dir()
    if not root.is_dir():
        return ()
    found = sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
    )
    return tuple(_installed(path, connections) for path in found)


def _installed(path: Path, connections: Connections) -> InstalledBundle:
    from arcagent.capabilities.artifact_signing import load_signature

    manifest_path = path / MANIFEST_NAME
    version = display = ""
    try:
        manifest = load_manifest(manifest_path.read_text(encoding="utf-8"), tier=Tier.PERSONAL)
        version, display = manifest.extension.version, manifest.extension.label
    except (OSError, ValueError, ExtensionError):
        display = path.name
    signature = load_signature(manifest_path)
    users = tuple(
        sorted(name for name, c in connections.connections().items() if c.extension == path.name)
    )
    return InstalledBundle(
        name=path.name,
        display_name=display or path.name,
        version=version,
        signer_did=signature.signer_did if signature is not None else "",
        used_by=users,
        path=path,
    )


def remove_installed_bundle(name: str, *, connections: Connections, audit_sink: AuditSink) -> None:
    """Delete one operator-installed bundle that no connection uses.

    Raises:
        ExtensionError: ``BUNDLE_REFUSED`` — ``not_installed`` (including every bundle
            Arc ships) or ``in_use`` with ``details["used_by"]``.
    """
    try:
        validate_extension_name(name)
        target = installed_extensions_dir() / name
        if target.is_symlink() or not (target / MANIFEST_NAME).is_file():
            raise _refuse("not_installed", f"{name!r} is not a package you installed")
        users = sorted(n for n, c in connections.connections().items() if c.extension == name)
        if users:
            raise _refuse(
                "in_use",
                f"{name!r} is used by {', '.join(users)}; remove those connections first",
                used_by=users,
            )
    except ValueError as exc:
        error = _refuse("not_installed", str(exc))
        _audit(audit_sink, "connector.bundle_removed", name, "deny", error, connections)
        raise error from exc
    except ExtensionError as exc:
        _audit(audit_sink, "connector.bundle_removed", name, "deny", exc, connections)
        raise
    shutil.rmtree(target)
    _audit(audit_sink, "connector.bundle_removed", name, "allow", None, connections)


def _audit(
    sink: AuditSink,
    action: str,
    name: str,
    outcome: str,
    error: ExtensionError | None,
    connections: Connections,
    **extra: str,
) -> None:
    """One event per verdict, through the single chokepoint (AU-2). Never a file's bytes."""
    from arctrust import causal

    details = {"bundle": name, **extra}
    if error is not None:
        details["reason"] = str(error.details.get("reason", error.code))
    emit(
        AuditEvent(
            actor_did=causal.actor_did(),
            action=action,
            target=f"bundle:{name}",
            outcome=outcome,
            tier=connections.world.tier.value,
            extra=details,
        ),
        sink,
    )


__all__ = [
    "BUNDLE_REFUSED",
    "MAX_ARCHIVE_BYTES",
    "MAX_ENTRIES",
    "MAX_EXPANDED_BYTES",
    "MAX_RATIO",
    "STAGING_TTL_SECONDS",
    "BundleDiff",
    "BundleFile",
    "BundleReview",
    "BundleStaging",
    "InstalledBundle",
    "Publisher",
    "SecretView",
    "StagedBundle",
    "ToolView",
    "UnsignedBundle",
    "installed_bundles",
    "read_archive",
    "read_tree",
    "remove_installed_bundle",
    "review_entries",
    "unsigned_local_bundles",
]
