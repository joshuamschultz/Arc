"""The operator's navigation GUIDE for one connection — an ``agents.md`` for a source.

The semantic layer (:mod:`arcmemory.semantic_layer`) says what a datastore's
tables and columns MEAN. Every other kind of connection had no place for the
knowledge only the person who owns it has: "/2. Areas/<Client> holds client
work; /Archive is old; prefer the newest file named FINAL". This module is that
place: one markdown file per connection, written by the operator in ArcUI and
read by every agent granted the connection.

**Instruction-adjacent, so protected like a prompt (LLM01/ASI06).** The guide is
copied into an agent's context, so it is signed with the deployment operator key
on every save (a detached ``.arcsig`` sidecar, exactly like the semantic layer)
and verified against the pinned operator key on every read. The agent path
(:func:`verified_guide`) hands out ONLY verified, signed bytes: an unsigned draft
is never injected, and a signed file whose bytes no longer match its signature
raises :class:`SourceGuideTamperedError` instead of degrading. Agents have no
write path at all; the only writer is :func:`write_guide`, which needs a signer.

**Connection-scoped.** One file per connection id, shared by every agent granted
it (consistent with the P18-4 shared stores). Access is the grant: a caller only
asks for the guides of connections it holds.

**Versioned.** Every save is also kept in ``<id>.guide.history/`` with its own
signature, so the current guide plus the last :data:`HISTORY_LIMIT` prior
versions are listed (signer, time, digest) and any of them can be restored. A
restore re-verifies the old bytes before re-signing them as the newest version,
so a file planted in the history folder can never be laundered into a signature.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from arcokf import read_regular_file
from arctrust.artifact import ArtifactSignature, verify_artifact
from pydantic import BaseModel, ConfigDict, Field

from arcmemory.mdfile import atomic_write_text, render_document

#: Detached-signature sidecar, the same suffix as the semantic layer and prompts.
SIGNATURE_SUFFIX = ".arcsig"
#: Hard ceiling on one guide. It is copied into prompts; 16 KB is pages of notes.
MAX_GUIDE_BYTES = 16 * 1024
#: Prior versions kept beside the current one.
HISTORY_LIMIT = 20
#: The OKF document a source's collection root carries for its guide. Connected
#: documents are always named ``<stem>-<hash>.md`` or ``<sha256>.md``, so this
#: name can never collide with a synced object.
GUIDE_DOCUMENT = "operator-guide.md"
#: The OKF ``type`` of that document — and so the heading its index section gets.
GUIDE_TYPE = "Operator guide"

_GUIDE_SUFFIX = ".guide.md"
_HISTORY_SUFFIX = ".guide.history"
_VERSION_FILE = re.compile(r"^(\d{6})\.md$")

#: Signs bytes as the operator. The caller resolves the operator's signing
#: capability; this module never sees key material.
GuideSigner = Callable[[bytes], ArtifactSignature]


class SourceGuideTamperedError(RuntimeError):
    """A signed guide's bytes no longer match its ``.arcsig`` sidecar.

    Raised rather than degrading: the guide steers an agent's navigation, so a
    signed file that fails verification is either corruption or an attacker
    editing instruction-adjacent text out from under the operator's signature.
    """


class SourceGuideTooLargeError(ValueError):
    """A guide over :data:`MAX_GUIDE_BYTES` was offered for signing."""


class SourceGuideVersionNotFoundError(LookupError):
    """A restore named a version that is not in the guide's history."""


class GuideVersion(BaseModel):
    """One saved version of a guide, as the history lists it."""

    model_config = ConfigDict(frozen=True)

    version: int
    signer: str
    updated_at: str
    digest: str


class SourceGuide(BaseModel):
    """One connection's guide as stored. ``content`` is empty when tampered."""

    model_config = ConfigDict(frozen=True)

    connection_id: str
    content: str = ""
    signed: bool = False
    signer: str | None = None
    updated_at: str | None = None
    version: int = 0
    tampered: bool = False
    digest: str = ""


class GuideFacts(BaseModel):
    """What Arc already knows about a connection, for a starter draft."""

    model_config = ConfigDict(frozen=True)

    connection_id: str
    name: str = ""
    kind: str = ""
    documents: int = 0
    #: Top-level folders (or resources) and how many documents each holds.
    folders: list[tuple[str, int]] = Field(default_factory=list)
    #: Titles of documents at the top level of the source.
    titles: list[str] = Field(default_factory=list)
    #: Datastore tables the semantic layer shows.
    tables: list[str] = Field(default_factory=list)


def _operator_public_key() -> bytes | None:
    """The deployment operator's Ed25519 verify key, or ``None`` if unavailable.

    The same pin the semantic layer verifies against: ArcUI signs a guide with
    the on-box operator key. Absent -> ``None``, which fails every signed read
    closed rather than skipping verification.
    """
    try:
        from arctrust import operator_public_key_for

        return operator_public_key_for()
    except (OSError, ValueError, RuntimeError):
        return None


# -- paths -------------------------------------------------------------------


def guide_path(connection_id: str, base: Path | str | None = None) -> Path | None:
    """One connection's guide file, beside its semantic layer; ``None`` if unnamed.

    Resolved through the semantic layer's own accessor, so the name is validated
    by the one resolver that owns ``<arc_config>/semantic`` and an id that is not
    a single safe path segment never reaches the filesystem.
    """
    from arctrust.paths import semantic_layer_file

    if not connection_id:
        return None
    try:
        layer = semantic_layer_file(connection_id, base)
    except ValueError:
        return None
    return layer.with_name(f"{connection_id}{_GUIDE_SUFFIX}")


def _sig(path: Path) -> Path:
    return path.with_name(path.name + SIGNATURE_SUFFIX)


def _history_dir(path: Path) -> Path:
    return path.with_name(path.name.removesuffix(_GUIDE_SUFFIX) + _HISTORY_SUFFIX)


def _version_path(history: Path, version: int) -> Path:
    return history / f"{version:06d}.md"


def _versions(history: Path) -> list[int]:
    if not history.is_dir():
        return []
    found = (_VERSION_FILE.match(entry.name) for entry in history.iterdir())
    return sorted(int(match.group(1)) for match in found if match is not None)


# -- verification ------------------------------------------------------------


def _read_verified(path: Path) -> tuple[bytes, ArtifactSignature]:
    """The bytes at ``path`` and their signature, verified; raise on any doubt.

    Read once, never through a symlink, bounded by :data:`MAX_GUIDE_BYTES`; the
    signature must verify against the pinned operator key.
    """
    try:
        content = read_regular_file(path)
        manifest = ArtifactSignature.from_json(read_regular_file(_sig(path)).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceGuideTamperedError(f"{path.name} could not be verified: {exc}") from exc
    if len(content) > MAX_GUIDE_BYTES:
        raise SourceGuideTamperedError(f"{path.name} is larger than {MAX_GUIDE_BYTES} bytes")
    if not verify_artifact(content, manifest, trusted_public_key=_pinned_key(path)):
        raise SourceGuideTamperedError(
            f"{path.name} failed signature verification against the pinned operator key "
            "— its content no longer matches the last signed save"
        )
    return content, manifest


def _pinned_key(path: Path) -> bytes:
    key = _operator_public_key()
    if key is None:
        raise SourceGuideTamperedError(f"{path.name}: no operator key is pinned to verify it")
    return key


def _is_signed(path: Path) -> bool:
    return _sig(path).exists() or _sig(path).is_symlink()


# -- the agent's read --------------------------------------------------------


def verified_guide(connection_id: str, base: Path | str | None = None) -> SourceGuide | None:
    """The guide an agent may be shown: signed and verified, or nothing.

    ``None`` when the connection has no guide or only an unsigned draft (never
    injected). Raises :class:`SourceGuideTamperedError` when a signed guide fails
    verification; the caller audits it and shows the agent no guide.
    """
    path = guide_path(connection_id, base)
    if path is None or not (path.exists() or path.is_symlink()):
        return None
    if not _is_signed(path):
        return None
    content, manifest = _read_verified(path)
    return _guide(connection_id, content, manifest, _current_version(path))


def _guide(
    connection_id: str, content: bytes, manifest: ArtifactSignature, version: int
) -> SourceGuide:
    return SourceGuide(
        connection_id=connection_id,
        content=content.decode("utf-8", errors="replace"),
        signed=True,
        signer=manifest.signer_did,
        updated_at=manifest.signed_at,
        version=version,
        digest=_hex(manifest.artifact_sha256),
    )


def _hex(digest: str) -> str:
    """The bare hex SHA-256 a signature manifest records (``sha256:<hex>``)."""
    return digest.removeprefix("sha256:")


def _current_version(path: Path) -> int:
    versions = _versions(_history_dir(path))
    return versions[-1] if versions else 0


# -- the operator's view -----------------------------------------------------


def read_guide(connection_id: str, base: Path | str | None = None) -> SourceGuide:
    """The guide as the operator sees it. Never raises on tamper; reports it.

    A tampered guide comes back with ``tampered=True`` and no content: the text
    an attacker wrote is not shown back as if it were the operator's. An
    unsigned file (placed by hand) is shown with ``signed=False``.
    """
    path = guide_path(connection_id, base)
    if path is None or not (path.exists() or path.is_symlink()):
        return SourceGuide(connection_id=connection_id)
    version = _current_version(path)
    if not _is_signed(path):
        try:
            text = read_regular_file(path)[: MAX_GUIDE_BYTES + 1].decode("utf-8", "replace")
        except OSError:
            text = ""
        return SourceGuide(connection_id=connection_id, content=text, version=version)
    try:
        content, manifest = _read_verified(path)
    except SourceGuideTamperedError:
        return SourceGuide(
            connection_id=connection_id, signed=True, tampered=True, version=version
        )
    return _guide(connection_id, content, manifest, version)


def guide_history(connection_id: str, base: Path | str | None = None) -> list[GuideVersion]:
    """Every kept version, newest first, as its signature sidecar records it."""
    path = guide_path(connection_id, base)
    if path is None:
        return []
    history = _history_dir(path)
    listed: list[GuideVersion] = []
    for version in reversed(_versions(history)):
        try:
            manifest = ArtifactSignature.from_json(
                read_regular_file(_sig(_version_path(history, version))).decode("utf-8")
            )
        except (OSError, ValueError):
            continue
        listed.append(
            GuideVersion(
                version=version,
                signer=manifest.signer_did,
                updated_at=manifest.signed_at or "",
                digest=_hex(manifest.artifact_sha256),
            )
        )
    return listed


# -- writes (operator only: they need a signer) ------------------------------


def write_guide(
    connection_id: str, content: str, sign: GuideSigner, base: Path | str | None = None
) -> SourceGuide:
    """Sign ``content`` as the operator and make it the connection's guide.

    The new version is written to the history first and then to the current
    file, each with its own signature; history is pruned to the current plus
    :data:`HISTORY_LIMIT` prior versions.
    """
    path = guide_path(connection_id, base)
    if path is None:
        raise ValueError(f"{connection_id!r} is not a usable connection name")
    data = content.encode("utf-8")
    if len(data) > MAX_GUIDE_BYTES:
        raise SourceGuideTooLargeError(
            f"a guide is at most {MAX_GUIDE_BYTES} bytes; this one is {len(data)}"
        )
    manifest = sign(data)
    if manifest.signed_at is None:
        # The time is metadata beside the signature, never part of what it covers.
        manifest = manifest.model_copy(update={"signed_at": datetime.now(UTC).isoformat()})
    history = _history_dir(path)
    history.mkdir(parents=True, exist_ok=True)
    version = _current_version(path) + 1
    _write_signed(_version_path(history, version), data, manifest)
    _write_signed(path, data, manifest)
    _prune(history)
    return _guide(connection_id, data, manifest, version)


def restore_guide(
    connection_id: str, version: int, sign: GuideSigner, base: Path | str | None = None
) -> SourceGuide:
    """Re-sign a kept version as the newest one, after verifying its old bytes."""
    path = guide_path(connection_id, base)
    if path is None:
        raise ValueError(f"{connection_id!r} is not a usable connection name")
    if version not in _versions(_history_dir(path)):
        raise SourceGuideVersionNotFoundError(f"guide version {version} is not kept")
    content, _ = _read_verified(_version_path(_history_dir(path), version))
    return write_guide(connection_id, content.decode("utf-8"), sign, base)


def _write_signed(path: Path, data: bytes, manifest: ArtifactSignature) -> None:
    """Write bytes then their signature, each atomically, owner-only."""
    atomic_write_text(path, data.decode("utf-8"))
    path.chmod(0o600)
    atomic_write_text(_sig(path), manifest.to_json())
    _sig(path).chmod(0o600)


def _prune(history: Path) -> None:
    for version in _versions(history)[: -(HISTORY_LIMIT + 1)]:
        old = _version_path(history, version)
        old.unlink(missing_ok=True)
        _sig(old).unlink(missing_ok=True)


# -- the starter draft -------------------------------------------------------

_STARTER_FOLDERS = 25
_STARTER_TITLES = 10
_STARTER_TABLES = 40


def render_starter_guide(facts: GuideFacts) -> str:
    """A draft guide from what Arc already knows; deterministic, no LLM.

    The same facts always give the same bytes (folders, titles and tables are
    sorted), so an operator who opens the starter twice sees one draft. Every
    line it fills in is a fact Arc observed; the rest are prompts for the
    operator, because an invented sentence about someone's files is worse than
    a blank one.
    """
    label = facts.name or facts.connection_id
    lines = [f"# Guide: {label}", ""]
    known = f"Arc sees {facts.documents} document" + ("" if facts.documents == 1 else "s")
    if facts.kind:
        known += f" in this {facts.kind} connection"
    lines += [known + ".", ""]
    if facts.folders:
        lines += ["## Layout", ""]
        for name, count in sorted(facts.folders)[:_STARTER_FOLDERS]:
            lines.append(f"- {name}/ ({count} doc{'' if count == 1 else 's'}): what lives here?")
        lines.append("")
    if facts.titles:
        lines += ["## At the top level", ""]
        lines += [f"- {title}" for title in sorted(facts.titles)[:_STARTER_TITLES]]
        lines.append("")
    if facts.tables:
        lines += ["## Tables", ""]
        lines += [
            f"- {table}: what one row is, and when to use it"
            for table in sorted(facts.tables)[:_STARTER_TABLES]
        ]
        lines.append("")
    lines += [
        "## How to navigate",
        "",
        "- Where does current work live? What is old or archived?",
        "- Which file wins when there are several versions (newest, or named FINAL)?",
        "- What should an agent never rely on here?",
        "",
    ]
    return "\n".join(lines)[:MAX_GUIDE_BYTES]


# -- the guide as an OKF document in a source's collection --------------------


def guide_document_text(guide: SourceGuide) -> str:
    """The verified guide as the OKF document a collection root carries.

    Its ``type`` is :data:`GUIDE_TYPE`, so the collection's root ``index.md``
    lists it under an "Operator guide" heading with the guide's first line. The
    ``generated`` stamp is the signing time, so an unchanged guide renders the
    same bytes every day and never churns the index.
    """
    body = guide.content.replace("[[", "[ [").strip() or "(empty guide)"
    stamp = (guide.updated_at or "")[:10] or "1970-01-01"
    return render_document(
        {
            "type": GUIDE_TYPE,
            "title": GUIDE_TYPE,
            "description": _first_line(guide.content),
            "guide_sha256": guide.digest,
            "guide_version": guide.version,
            "signer": guide.signer or "",
            "generated": {"by": "operator", "at": stamp},
        },
        "Navigation guidance written and signed by the operator for this source.\n\n" + body,
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        clean = " ".join(line.strip().lstrip("#-* ").split())
        if clean:
            return clean[:200]
    return GUIDE_TYPE


def sync_guide_document(
    collection_root: Path, connection_id: str, base: Path | str | None = None
) -> bool:
    """Bring ``collection_root/operator-guide.md`` in line with the verified guide.

    Returns whether the file changed (written or removed), so the caller only
    rebuilds the routing index when it did. No guide, or only an unsigned draft,
    means no document. A tampered guide removes the document and then raises
    :class:`SourceGuideTamperedError`, so the old text stops being served even
    though the new text is refused.
    """
    target = collection_root / GUIDE_DOCUMENT
    try:
        guide = verified_guide(connection_id, base)
    except SourceGuideTamperedError:
        _remove(target)
        raise
    if guide is None:
        return _remove(target)
    text = guide_document_text(guide)
    if _current_text(target) == text:
        return False
    atomic_write_text(target, text)
    return True


def _current_text(path: Path) -> str | None:
    try:
        return read_regular_file(path).decode("utf-8")
    except (OSError, UnicodeError):
        return None


def _remove(path: Path) -> bool:
    if not (path.exists() or path.is_symlink()):
        return False
    path.unlink()
    return True


__all__ = [
    "GUIDE_DOCUMENT",
    "GUIDE_TYPE",
    "HISTORY_LIMIT",
    "MAX_GUIDE_BYTES",
    "SIGNATURE_SUFFIX",
    "GuideFacts",
    "GuideSigner",
    "GuideVersion",
    "SourceGuide",
    "SourceGuideTamperedError",
    "SourceGuideTooLargeError",
    "SourceGuideVersionNotFoundError",
    "guide_document_text",
    "guide_history",
    "guide_path",
    "read_guide",
    "render_starter_guide",
    "restore_guide",
    "sync_guide_document",
    "verified_guide",
    "write_guide",
]
