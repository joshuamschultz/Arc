"""MediaStore — custody of every artefact crossing the gateway boundary.

SPEC-065 COMP-002 (REQ-297, REQ-298, REQ-299, REQ-300).

Three invariants hold this module together:

ADR-029 — an inbound attachment is *agent state*, so it is written with direct
filesystem I/O into the agent's workspace. It never travels through the
LLM-facing ``write``/``bash``/``edit`` tools, which would let the agent's brain
leak into whatever project directory the tools happen to be pointed at.

REQ-298 — the sender names the file, the **gateway** composes the path. The
declared name is untrusted remote input (LLM05 improper output handling, ASI02
tool misuse) and is retained as metadata only; it contributes a sanitised stem
and extension and nothing else. See ``_safe_stem_and_ext`` for the reasoning.

REQ-300 — one audit event per artefact, through the gateway's arctrust
chokepoint. The event records *custody*: who, which channel, what kind, how
big, and where it landed. Never the bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from arctrust.classification import dominates, parse_classification
from pydantic import BaseModel, ConfigDict

from arcgateway.attachment_scanner import AttachmentScanner, CleanScanner, ScanStatus
from arcgateway.audit import emit_event

# POSIX NAME_MAX: a single path component may not exceed 255 bytes. Every
# component we compose is restricted to _SAFE_CHARS, which is ASCII, so byte
# length and character length are the same and slicing cannot split a rune.
_NAME_MAX = 255

# Anything outside this set is remote-controlled noise: separators, control
# characters, quoting, shell metacharacters, unicode homoglyphs. Collapse it.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Bounds on the parts the sender influences, so the composed name always has
# room left for the gateway's own prefix inside _NAME_MAX.
_MAX_SENDER = 64
_MAX_EXT = 16

# Used when the declared name yields nothing usable ("...", "", "/etc/").
_FALLBACK_STEM = "file"

# Distinct artefacts can share a second, a sender and a declared name (a photo
# album). Rather than overwrite one with the other, disambiguate — bounded, so
# a flood cannot spin here forever.
_MAX_COLLISIONS = 1000

_PDF_MAGIC = b"%PDF-"


class MediaTooLargeError(Exception):
    """An inbound artefact exceeded the configured ceiling and was refused.

    Typed rather than generic because the caller has to answer the sender on
    the channel the artefact arrived from (REQ-299), which needs all four
    fields carried here.
    """

    def __init__(
        self,
        *,
        channel: str,
        declared_name: str,
        size_bytes: int,
        limit_bytes: int,
    ) -> None:
        super().__init__(
            f"artefact {declared_name!r} from {channel} is {size_bytes} bytes, "
            f"over the {limit_bytes} byte ceiling"
        )
        self.channel = channel
        self.declared_name = declared_name
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


class StoredMedia(BaseModel):
    """Where an artefact landed, and what the sender claimed it was."""

    model_config = ConfigDict(frozen=True)

    path: Path
    """Absolute path the gateway composed and wrote to."""

    ref: str
    """``path`` relative to the workspace root — what travels in a MediaPart.

    Relative because a reference outlives the machine that made it: history is
    replayed, moved between hosts and rendered in a browser, and an absolute
    path would both break on relocation and publish the host's filesystem
    layout to every one of those readers.
    """

    declared_name: str
    """Verbatim sender-supplied filename — metadata only, never a path input."""

    kind: str
    """Artefact class: ``image``, ``file``, ``audio`` — the MediaPart vocabulary."""

    mime: str
    """Declared MIME type."""

    size_bytes: int
    """Bytes written."""


class AttachmentManifest(BaseModel):
    """Durable, reference-only metadata for a quarantined or promoted file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attachment_id: str
    agent_did: str
    owner_did: str
    session_key: str
    workspace_ref: str
    declared_name: str
    detected_mime: str
    kind: str
    size_bytes: int
    sha256: str
    classification: str
    scan_status: ScanStatus
    created_at: datetime
    expires_at: datetime | None = None


class AttachmentError(Exception):
    """Base class for fail-closed attachment errors."""


class AttachmentValidationError(AttachmentError):
    """The bytes or metadata failed validation."""


class AttachmentClaimError(AttachmentError):
    """The caller cannot claim the attachment."""


class AttachmentQuotaError(AttachmentError):
    """The file or workspace quota would be exceeded."""


class AttachmentByteStream(Protocol):
    """Async byte source consumed incrementally by :meth:`store_stream`."""

    def __aiter__(self) -> AsyncIterator[bytes]: ...


def _sanitise(raw: str) -> str:
    """Collapse untrusted text to a single safe path-component fragment.

    Unsafe runs become a hyphen rather than vanishing, so ``a/b`` cannot
    silently become the different-looking ``ab``. Leading and trailing
    ``-._`` are then stripped, which is what disarms the dot-only names
    (``..``, ``...``) that would otherwise mean "parent directory".
    """
    return _UNSAFE_CHARS.sub("-", raw).strip("-._")


def _safe_stem_and_ext(declared_name: str) -> tuple[str, str]:
    """Derive a stem and extension from an untrusted declared filename.

    The declared name is treated as data, not as a path, at every step:

    1. Both separator conventions are folded and only the last component is
       kept, so ``../../../etc/passwd`` and ``..\\..\\x`` contribute a leaf.
    2. Non-printables go, which removes NUL bytes (filename truncation in the
       C layer) and CR/LF (audit- and log-injection).
    3. The remainder is collapsed to ``_SAFE_CHARS``; anything left over that
       still means nothing falls back to ``_FALLBACK_STEM``.

    Length is *not* bounded here — the final bound depends on the prefix the
    gateway prepends, so ``_compose_name`` owns it.
    """
    leaf = declared_name.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = "".join(char for char in leaf if char.isprintable())

    stem, dot, ext = leaf.rpartition(".")
    if not dot:  # rpartition puts the whole string in `ext` when there is none
        stem, ext = leaf, ""

    return _sanitise(stem) or _FALLBACK_STEM, _sanitise(ext)[:_MAX_EXT]


class MediaStore:
    """Writes inbound artefacts into an agent workspace and audits both directions.

    Args:
        workspace: The agent's workspace root. Artefacts land under
            ``<workspace>/inbox/<YYYY-MM-DD>/``.
        max_bytes: Inclusive size ceiling; an artefact exactly at the ceiling
            is accepted.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        max_bytes: int,
        scanner: AttachmentScanner | None = None,
        max_files: int | None = None,
        max_total_bytes: int | None = None,
        clearance: str = "UNCLASSIFIED",
    ) -> None:
        self._workspace = workspace
        self._inbox = workspace / "inbox"
        self._attachments = workspace / "attachments"
        self._quarantine = self._attachments / "quarantine"
        self._objects = self._attachments / "objects"
        self._max_bytes = max_bytes
        self._scanner = scanner or CleanScanner()
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes
        self._clearance = parse_classification(clearance, strict=False)

    def _assert_workspace_dir(self, path: Path) -> None:
        """Reject a pre-existing symlinked storage root before opening files."""
        root = self._workspace.resolve()
        resolved = path.resolve()
        if root not in resolved.parents and resolved != root:
            raise AttachmentValidationError("attachment storage escaped workspace")

    @property
    def max_bytes(self) -> int:
        """The ceiling, readable so a caller can refuse before spending bytes."""
        return self._max_bytes

    async def store_stream(
        self,
        *,
        stream: AttachmentByteStream,
        declared_name: str,
        declared_mime: str | None,
        kind: str,
        owner_did: str,
        agent_did: str,
        classification: str = "UNCLASSIFIED",
        expires_at: datetime | None = None,
        **identity: str,
    ) -> AttachmentManifest:
        """Stream an upload into quarantine, validate it, then promote atomically.

        The stream is never collected into a ``bytes`` object. A failed or
        rejected upload is removed from quarantine and is never claimable.
        """
        if self._max_files is not None and self._count_manifests() >= self._max_files:
            raise AttachmentQuotaError("attachment file quota exceeded")
        resource_class = parse_classification(classification, strict=False)
        if not dominates(self._clearance, resource_class):
            raise AttachmentClaimError("attachment classification exceeds workspace clearance")

        if self._attachments.is_symlink():
            raise AttachmentValidationError("attachment storage root is a symlink")
        self._attachments.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_workspace_dir(self._attachments)
        self._quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._objects.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_workspace_dir(self._quarantine)
        self._assert_workspace_dir(self._objects)
        self._attachments.chmod(0o700)
        self._quarantine.chmod(0o700)
        self._objects.chmod(0o700)
        attachment_id = f"att_{uuid.uuid4().hex}"
        temporary = self._quarantine / f".{attachment_id}.upload"
        digest = hashlib.sha256()
        size = 0
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                async for chunk in stream:
                    if not isinstance(chunk, bytes):
                        raise AttachmentValidationError("attachment stream yielded non-bytes")
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise MediaTooLargeError(
                            channel="arcui",
                            declared_name=declared_name,
                            size_bytes=size,
                            limit_bytes=self._max_bytes,
                        )
                    if (
                        self._max_total_bytes is not None
                        and self._stored_total_bytes() + size > self._max_total_bytes
                    ):
                        raise AttachmentQuotaError("attachment byte quota exceeded")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            detected_mime = _detect_mime(temporary)
            if not _mime_matches(declared_mime, detected_mime):
                raise AttachmentValidationError(
                    f"declared MIME {declared_mime!r} does not match {detected_mime!r}"
                )
            sha256 = f"sha256:{digest.hexdigest()}"
            emit_event(
                "attachment.validated",
                attachment_id,
                "allow",
                actor_did=owner_did,
                extra={"agent_did": agent_did, "mime": detected_mime, "sha256": sha256},
            )
            verdict = await self._scanner.scan(temporary, mime=detected_mime, sha256=sha256)
            if verdict is not ScanStatus.CLEAN:
                raise AttachmentValidationError(f"attachment scan failed: {verdict.value}")

            object_dir = self._objects / digest.hexdigest()[:2]
            object_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._assert_workspace_dir(object_dir)
            object_dir.chmod(0o700)
            target = object_dir / digest.hexdigest()
            if target.is_symlink():
                raise AttachmentValidationError("attachment object is a symlink")
            if target.exists():
                temporary.unlink()
            else:
                temporary.replace(target)
                target.chmod(0o600)
            ref = target.relative_to(self._workspace).as_posix()
            manifest = AttachmentManifest(
                attachment_id=attachment_id,
                agent_did=agent_did,
                owner_did=owner_did,
                session_key=identity["session_key"],
                workspace_ref=ref,
                declared_name=declared_name,
                detected_mime=detected_mime,
                kind=kind,
                size_bytes=size,
                sha256=sha256,
                classification=resource_class.name,
                scan_status=verdict,
                created_at=datetime.now(UTC),
                expires_at=expires_at,
            )
            self._write_manifest(manifest)
            emit_event(
                "attachment.received",
                attachment_id,
                "allow",
                actor_did=owner_did,
                extra={"agent_did": agent_did, "sha256": sha256, "size_bytes": size},
            )
            return manifest
        except BaseException as exc:
            temporary.unlink(missing_ok=True)
            emit_event(
                "attachment.rejected",
                attachment_id,
                "deny",
                actor_did=owner_did,
                extra={"agent_did": agent_did, "reason": type(exc).__name__},
            )
            raise

    def claim(
        self,
        *,
        attachment_id: str,
        owner_did: str,
        agent_did: str,
        session_key: str,
        clearance: str | None = None,
    ) -> StoredMedia:
        """Return a clean attachment only to its bound identity and session."""
        manifest = self._read_manifest(attachment_id)
        if manifest is None:
            raise AttachmentClaimError("attachment not found")
        if manifest.scan_status is not ScanStatus.CLEAN:
            raise AttachmentClaimError("attachment is not clean")
        if manifest.owner_did != owner_did or manifest.agent_did != agent_did:
            raise AttachmentClaimError("attachment identity mismatch")
        if manifest.session_key != session_key:
            raise AttachmentClaimError("attachment session mismatch")
        subject = parse_classification(clearance or self._clearance.name, strict=False)
        resource = parse_classification(manifest.classification, strict=True)
        if not dominates(subject, resource):
            raise AttachmentClaimError("attachment classification denied")
        path = (self._workspace / manifest.workspace_ref).resolve()
        if self._workspace.resolve() not in path.parents or not path.is_file():
            raise AttachmentClaimError("attachment reference escaped workspace")
        emit_event(
            "attachment.claimed",
            attachment_id,
            "allow",
            actor_did=owner_did,
            extra={"agent_did": agent_did, "session_key": session_key},
        )
        return StoredMedia(
            path=path,
            ref=manifest.workspace_ref,
            declared_name=manifest.declared_name,
            kind=manifest.kind,
            mime=manifest.detected_mime,
            size_bytes=manifest.size_bytes,
        )

    def _manifest_path(self, attachment_id: str) -> Path:
        if not re.fullmatch(r"att_[0-9a-f]{32}", attachment_id):
            raise AttachmentClaimError("invalid attachment ID")
        return self._attachments / "manifests" / f"{attachment_id}.json"

    def _write_manifest(self, manifest: AttachmentManifest) -> None:
        directory = self._attachments / "manifests"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._manifest_path(manifest.attachment_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(manifest.model_dump_json(), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def _read_manifest(self, attachment_id: str) -> AttachmentManifest | None:
        path = self._manifest_path(attachment_id)
        if not path.is_file():
            return None
        return AttachmentManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def _count_manifests(self) -> int:
        directory = self._attachments / "manifests"
        return len(list(directory.glob("att_*.json"))) if directory.exists() else 0

    def _stored_total_bytes(self) -> int:
        if not self._objects.exists():
            return 0
        return sum(path.stat().st_size for path in self._objects.rglob("*") if path.is_file())

    def store(
        self,
        *,
        data: bytes,
        declared_name: str,
        mime: str,
        kind: str,
        sender: str,
        channel: str,
        actor_did: str,
    ) -> StoredMedia:
        """Write one inbound artefact to the workspace and audit its arrival.

        Args:
            data: The artefact bytes.
            declared_name: What the sender called it. Untrusted.
            mime: Declared MIME type.
            kind: Artefact class (``image``, ``file``, ``audio``).
            sender: Resolved sender identity, used in the composed filename.
            channel: Channel the artefact arrived on, e.g. ``telegram:9001``.
            actor_did: DID of the entity that sent it.

        Returns:
            Where the bytes landed, plus the declared metadata.

        Raises:
            MediaTooLargeError: The artefact is over the ceiling. Nothing is
                written and no ``media.received`` event is emitted — the
                refusal is decided before any file is opened, so there is no
                window in which a rejected artefact exists on disk.
        """
        size_bytes = len(data)
        if size_bytes > self._max_bytes:
            raise MediaTooLargeError(
                channel=channel,
                declared_name=declared_name,
                size_bytes=size_bytes,
                limit_bytes=self._max_bytes,
            )

        path = self._write(data=data, declared_name=declared_name, sender=sender)
        detected_mime = _detect_pdf_mime(data, declared_mime=mime)

        emit_event(
            "media.received",
            str(path),
            "allow",
            actor_did=actor_did,
            extra={
                "channel": channel,
                "kind": kind,
                "mime": detected_mime,
                "size_bytes": size_bytes,
                "declared_name": declared_name,
            },
        )

        return StoredMedia(
            path=path,
            ref=path.relative_to(self._workspace).as_posix(),
            declared_name=declared_name,
            kind=kind,
            mime=detected_mime,
            size_bytes=size_bytes,
        )

    def record_sent(
        self,
        *,
        path: Path,
        mime: str,
        kind: str,
        channel: str,
        actor_did: str,
    ) -> None:
        """Audit one artefact leaving the agent for a channel (REQ-300).

        Args:
            path: The workspace file that was sent.
            mime: MIME type sent to the platform.
            kind: Artefact class.
            channel: Channel it was sent on.
            actor_did: DID of the agent that sent it.
        """
        emit_event(
            "media.sent",
            str(path),
            "allow",
            actor_did=actor_did,
            extra={
                "channel": channel,
                "kind": kind,
                "mime": mime,
                "size_bytes": path.stat().st_size,
            },
        )

    def _write(self, *, data: bytes, declared_name: str, sender: str) -> Path:
        """Compose the path, prove containment, then write directly (ADR-029)."""
        now = datetime.now(UTC)
        day_dir = self._inbox / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        stem, ext = _safe_stem_and_ext(declared_name)
        safe_sender = _sanitise(sender)[:_MAX_SENDER] or "unknown"
        clock = now.strftime("%H%M%S")

        for ordinal in range(1, _MAX_COLLISIONS + 1):
            name = _compose_name(
                clock=clock, sender=safe_sender, stem=stem, ext=ext, ordinal=ordinal
            )
            path = day_dir / name
            self._assert_inside_inbox(path)
            try:
                # Exclusive create: the collision check and the claim are one
                # atomic step, so two concurrent artefacts cannot both win.
                with path.open("xb") as handle:
                    handle.write(data)
            except FileExistsError:
                continue
            return path

        raise FileExistsError(f"{_MAX_COLLISIONS} artefacts already share {day_dir / name}")

    def _assert_inside_inbox(self, path: Path) -> None:
        """Belt and braces: prove by ancestry that the target is inside the inbox.

        Sanitisation should already make escape impossible, but a string prefix
        check would happily accept ``inbox/../../etc/passwd``. Resolving first
        and testing genuine ancestry is the check that cannot be talked around.
        """
        resolved = path.resolve()
        if self._inbox.resolve() not in resolved.parents:
            raise ValueError(f"composed path {resolved} escaped the inbox")


def _compose_name(*, clock: str, sender: str, stem: str, ext: str, ordinal: int) -> str:
    """Build the single filename component, bounded to NAME_MAX.

    The gateway's own fields (time, sender, collision ordinal, extension) are
    laid down first and the sender-influenced stem absorbs whatever budget is
    left, so a 500-character declared name shortens the stem instead of
    pushing the filename past what the filesystem will accept.
    """
    tag = "" if ordinal == 1 else f"-{ordinal}"
    suffix = f".{ext}" if ext else ""
    prefix = f"{clock}-{sender}-"
    budget = _NAME_MAX - len(prefix) - len(tag) - len(suffix)
    return f"{prefix}{stem[:budget]}{tag}{suffix}"


def _detect_pdf_mime(data: bytes, *, declared_mime: str) -> str:
    """Resolve stable content types from bytes before preserving metadata.

    Custody paths are implementation details and may be digest-only in other
    stores, so extension-based detection is intentionally absent.  Unknown
    formats retain the platform declaration for backward compatibility; PDFs
    are authoritative because their magic header is unambiguous.
    """
    if data.startswith(_PDF_MAGIC) or data.startswith(b"\xef\xbb\xbf%PDF-"):
        return "application/pdf"
    return declared_mime


_MAGIC_MIMES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"\xef\xbb\xbf%PDF-", "application/pdf"),
    (b"RIFF", "audio/wav"),
    (b"ID3", "audio/mpeg"),
)


def _detect_mime(path: Path) -> str:
    """Detect the small allowlisted set without invoking a document parser."""
    with path.open("rb") as handle:
        prefix = handle.read(16)
    for magic, mime in _MAGIC_MIMES:
        if prefix.startswith(magic):
            return mime
    return "application/octet-stream"


def _mime_matches(declared: str | None, detected: str) -> bool:
    """Allow omitted declarations, but reject a supplied spoofed declaration."""
    return (
        declared is None
        or declared == detected
        or (declared == "image/jpg" and detected == "image/jpeg")
    )


__all__ = [
    "AttachmentByteStream",
    "AttachmentClaimError",
    "AttachmentError",
    "AttachmentManifest",
    "AttachmentQuotaError",
    "AttachmentValidationError",
    "MediaStore",
    "MediaTooLargeError",
    "StoredMedia",
]
