"""Per-folder OKF ``index.md`` rendering and verification.

Every folder of a bundle may carry a reserved ``index.md``: a grouped listing
(``# Group`` headings, ``* [Title](url) - description`` lines) an agent reads to
see what a folder holds before opening any file. Only the bundle-root index may
carry frontmatter, and only ``okf_version``.

The visible listing stays spec-shaped. Integrity lives beside it in a
``.index.digest`` sidecar (the index's own SHA-256, each listed document's
SHA-256, and the child-folder counts), so a reader can fail closed on a
tampered index in O(1) and on a changed document in O(folder). ``arcokf`` only
renders, parses and validates; the owning store decides when to write.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from .core import ROOT_INDEX_METADATA, VERSION, lint, validate

INDEX_NAME = "index.md"
DIGEST_NAME = ".index.digest"
FOLDERS_GROUP = "Folders"

_RESERVED_NAMES = frozenset({"index.md", "context.md", "log.md"})
_OPERATIONAL_DIRS = frozenset(
    {".git", ".arc", ".codex", "audit", "audits", "config", "secrets", "credentials"}
)
_ROOT_FRONTMATTER = f'---\nokf_version: "{VERSION}"\n---\n'
_MAX_TITLE = 120
_MAX_DESCRIPTION = 200
_LINE_RE = re.compile(r"^\* \[([^\]]*)\]\(([^)]*)\)(?: - (.*))?$")
_LABEL_RE = re.compile(r"^(.*?)\s*\(classification: ([^)]*)\)$")
_FOLDER_COUNT_RE = re.compile(r"^(\d+) docs?$")


class FolderIndexError(ValueError):
    """Raised when an index entry or index text cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One line of a folder index: a document, or a child folder's own index.

    ``digest`` (documents only) and ``count`` (folders only) are integrity data
    that live in the sidecar, never in the visible line.
    """

    path: str
    title: str
    description: str = ""
    group: str = ""
    classification: str = ""
    digest: str = ""
    count: int = 0

    @property
    def is_folder(self) -> bool:
        """True for a child-folder line (``name/index.md``)."""
        return self.group == FOLDERS_GROUP


@dataclass(frozen=True, slots=True)
class FolderDigest:
    """The parsed ``.index.digest`` sidecar of one folder."""

    index: str
    docs: dict[str, str]
    folders: dict[str, int]

    @property
    def count(self) -> int:
        """Total documents under the folder, recursively."""
        return len(self.docs) + sum(self.folders.values())


@dataclass(frozen=True, slots=True)
class FolderIndexValidation:
    """Structured result of verifying one folder's index."""

    valid: bool
    error: str = ""
    entries: tuple[IndexEntry, ...] = ()


def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value).split())[:limit]


def folder_summary_entry(name: str, count: int) -> IndexEntry:
    """The parent's line for child folder ``name`` holding ``count`` documents."""
    return IndexEntry(
        path=f"{name}/{INDEX_NAME}",
        title=f"{name}/",
        description=f"{count} doc" + ("" if count == 1 else "s"),
        group=FOLDERS_GROUP,
        count=count,
    )


def listable_dir(name: str) -> bool:
    """Whether a child directory belongs in an index (not hidden, not operational)."""
    return not name.startswith(".") and name.lower() not in _OPERATIONAL_DIRS


def listable_file(name: str) -> bool:
    """Whether a file is a concept document an index may list."""
    return (
        name.lower().endswith(".md") and name not in _RESERVED_NAMES and not name.startswith(".")
    )


def folder_entry(path: Path) -> IndexEntry | None:
    """Build the index entry for one document, or ``None`` if it is not valid OKF.

    Every valid document is listed, whatever its classification: the entry
    carries the label so a reader can gate on it. Title and description come
    from frontmatter (``title``/``name``; ``description``/``summary``/
    ``when_to_use``/``trigger``) and fall back to the first heading and the
    first prose line.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    result = validate(raw, path=path.as_posix())
    if not result.valid or result.document is None:
        return None
    metadata, body = result.document.metadata, result.document.body
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    heading = next((line.lstrip("# ").strip() for line in lines if line.startswith("#")), "")
    prose = next((line.lstrip("-* ").strip() for line in lines if not line.startswith("#")), "")
    title = next(
        (str(metadata[key]) for key in ("title", "name") if metadata.get(key)),
        heading or path.stem,
    )
    description = next(
        (
            str(metadata[key])
            for key in ("description", "summary", "when_to_use", "trigger")
            if metadata.get(key)
        ),
        prose,
    )
    return IndexEntry(
        path=path.name,
        title=title,
        description=description,
        group=str(metadata["type"]).strip(),
        classification=str(metadata.get("classification") or "").strip().lower(),
        digest=hashlib.sha256(raw).hexdigest(),
    )


def _normalised(entry: IndexEntry) -> IndexEntry:
    """The entry exactly as it round-trips through the rendered line."""
    path = PurePosixPath(entry.path)
    if not entry.path or path.is_absolute() or ".." in path.parts:
        raise FolderIndexError(f"index path is not relative: {entry.path!r}")
    if entry.is_folder:
        if path.parts[-1] != INDEX_NAME or len(path.parts) != 2:
            raise FolderIndexError(f"folder entry must be <name>/index.md: {entry.path!r}")
    elif path.suffix.lower() != ".md" or path.name in _RESERVED_NAMES or len(path.parts) != 1:
        raise FolderIndexError(f"index path is not a listable document: {entry.path!r}")
    title = _one_line(entry.title, _MAX_TITLE).replace("[", "(").replace("]", ")")
    if not title:
        raise FolderIndexError(f"index title is empty: {entry.path!r}")
    group = _one_line(entry.group, _MAX_TITLE) or "Documents"
    label = _one_line(entry.classification, _MAX_TITLE).lower().replace(")", "")
    description = _one_line(entry.description, _MAX_DESCRIPTION)
    return replace(entry, title=title, group=group, classification=label, description=description)


def _line(entry: IndexEntry) -> str:
    suffix = entry.description
    if entry.classification:
        suffix = f"{suffix} (classification: {entry.classification})".strip()
    tail = f" - {suffix}" if suffix else ""
    return f"* [{entry.title}]({quote(entry.path, safe='/')}){tail}"


def render_folder_index(entries: list[IndexEntry] | tuple[IndexEntry, ...], *, root: bool) -> str:
    """Render a folder's index: folders first, then one ``# <type>`` group per type.

    ``root=True`` adds the only frontmatter OKF allows on an index, the
    ``okf_version`` of the bundle root. The result is a pure function of the
    entries, so identical inventories always produce identical bytes.
    """
    ordered = sorted((_normalised(entry) for entry in entries), key=lambda e: e.path)
    if len({entry.path for entry in ordered}) != len(ordered):
        raise FolderIndexError("index contains duplicate paths")
    groups: dict[str, list[IndexEntry]] = {}
    for entry in ordered:
        groups.setdefault(entry.group, []).append(entry)
    names = sorted(groups, key=lambda name: (name != FOLDERS_GROUP, name))
    blocks = [f"# {name}\n" + "\n".join(_line(entry) for entry in groups[name]) for name in names]
    body = "\n\n".join(blocks) if blocks else "# Documents\n"
    return (_ROOT_FRONTMATTER if root else "") + body.rstrip("\n") + "\n"


def parse_folder_index(text: str, *, root: bool) -> tuple[IndexEntry, ...]:
    """Parse index text back into entries; reject anything that is not canonical."""
    entries: list[IndexEntry] = []
    group = ""
    body = text
    if root:
        if not text.startswith(_ROOT_FRONTMATTER):
            raise FolderIndexError("root index must carry okf_version frontmatter")
        body = text[len(_ROOT_FRONTMATTER) :]
    for line in body.splitlines():
        if not line:
            continue
        if line.startswith("# "):
            group = line[2:]
            continue
        match = _LINE_RE.match(line)
        if match is None or not group:
            raise FolderIndexError("unexpected folder index content")
        title, target, tail = match.group(1), unquote(match.group(2)), match.group(3) or ""
        label = ""
        labelled = _LABEL_RE.match(tail)
        if labelled is not None:
            tail, label = labelled.group(1), labelled.group(2)
        count = 0
        if group == FOLDERS_GROUP:
            counted = _FOLDER_COUNT_RE.match(tail)
            if counted is None:
                raise FolderIndexError("folder entry lacks a document count")
            count = int(counted.group(1))
        entries.append(IndexEntry(target, title, tail, group, label, count=count))
    if render_folder_index(entries, root=root) != text:
        raise FolderIndexError("folder index is not canonical or was tampered")
    return tuple(entries)


def render_folder_digest(index_text: str, entries: tuple[IndexEntry, ...]) -> str:
    """The sidecar for ``index_text``: its own digest, each document's, folder counts."""
    payload = {
        "docs": {entry.path: entry.digest for entry in entries if not entry.is_folder},
        "folders": {
            entry.path.split("/", 1)[0]: entry.count for entry in entries if entry.is_folder
        },
        "index": hashlib.sha256(index_text.encode("utf-8")).hexdigest(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def parse_folder_digest(text: str) -> FolderDigest:
    """Parse a sidecar; any malformed shape is an error, never a partial trust."""
    try:
        raw = json.loads(text)
        docs = {str(k): str(v) for k, v in dict(raw["docs"]).items()}
        folders = {str(k): int(v) for k, v in dict(raw["folders"]).items()}
        index = str(raw["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FolderIndexError("invalid folder digest sidecar") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", index) or not all(
        re.fullmatch(r"[0-9a-f]{64}", digest) for digest in docs.values()
    ):
        raise FolderIndexError("invalid folder digest sidecar")
    return FolderDigest(index=index, docs=docs, folders=folders)


def read_folder_digest(folder: Path) -> FolderDigest | None:
    """The folder's sidecar, or ``None`` when absent or unreadable."""
    try:
        return parse_folder_digest((folder / DIGEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, FolderIndexError):
        return None


def validate_folder_index(
    folder: Path, *, root: bool = False, deep: bool = False
) -> FolderIndexValidation:
    """Verify ONE folder's index (never its descendants).

    Shallow (default, O(1) plus the index size): the index is canonical, carries
    the right frontmatter, and matches its sidecar byte for byte. Deep adds
    O(folder): every listed document still exists with its recorded digest, no
    valid document is missing from the listing, and child-folder counts match
    the children's own sidecars.
    """
    try:
        text = (folder / INDEX_NAME).read_text(encoding="utf-8")
        digest = parse_folder_digest((folder / DIGEST_NAME).read_text(encoding="utf-8"))
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest.index:
            raise FolderIndexError("folder index does not match its digest")
        if not lint(folder / INDEX_NAME, bundle_root=root).valid:
            raise FolderIndexError("folder index is not valid OKF")
        entries = parse_folder_index(text, root=root)
        listed_docs = {entry.path for entry in entries if not entry.is_folder}
        if listed_docs != set(digest.docs):
            raise FolderIndexError("folder index and digest list different documents")
        if deep:
            entries = _verify_deep(folder, entries, digest)
        return FolderIndexValidation(True, entries=entries)
    except (FolderIndexError, OSError, UnicodeError) as exc:
        return FolderIndexValidation(False, error=str(exc))


def _listing(entry: IndexEntry) -> tuple[str, str, str, str]:
    return (entry.title, entry.description, entry.group, entry.classification)


def _verify_deep(
    folder: Path, entries: tuple[IndexEntry, ...], digest: FolderDigest
) -> tuple[IndexEntry, ...]:
    on_disk = {path.name for path in folder.glob("*.md") if listable_file(path.name)}
    verified: list[IndexEntry] = []
    for entry in entries:
        if entry.is_folder:
            child = read_folder_digest(folder / entry.path.split("/", 1)[0])
            if child is None or child.count != entry.count:
                raise FolderIndexError(f"folder listing is stale: {entry.path}")
            verified.append(entry)
            continue
        current = folder_entry(folder / entry.path)
        if current is None:
            raise FolderIndexError(f"indexed document is missing or invalid: {entry.path}")
        if current.digest != digest.docs[entry.path]:
            raise FolderIndexError(f"indexed document digest mismatch: {entry.path}")
        if _listing(_normalised(replace(current, path=entry.path))) != _listing(entry):
            raise FolderIndexError(f"index line differs from its document: {entry.path}")
        verified.append(replace(entry, digest=current.digest))
    unlisted = {name for name in on_disk if name not in digest.docs}
    if any(folder_entry(folder / name) is not None for name in sorted(unlisted)):
        raise FolderIndexError("folder index inventory is stale")
    return tuple(verified)


__all__ = [
    "DIGEST_NAME",
    "FOLDERS_GROUP",
    "INDEX_NAME",
    "ROOT_INDEX_METADATA",
    "FolderDigest",
    "FolderIndexError",
    "FolderIndexValidation",
    "IndexEntry",
    "folder_entry",
    "folder_summary_entry",
    "listable_dir",
    "listable_file",
    "parse_folder_digest",
    "parse_folder_index",
    "read_folder_digest",
    "render_folder_digest",
    "render_folder_index",
    "validate_folder_index",
]
