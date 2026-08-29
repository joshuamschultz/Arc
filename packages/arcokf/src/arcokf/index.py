"""Deterministic, hash-verifiable collection indexes for OKF documents.

The index is deliberately a plain reserved ``index.md`` document.  Its machine
readable entry comments make validation unambiguous while the adjacent Markdown
links remain useful to people.  ``arcokf`` only renders and validates; the
owning store is responsible for deciding when to write one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .core import lint

_HEADER = "# Collection Index\n\n<!-- arcokf:collection-index:v1 -->"
_INVENTORY_RE = re.compile(r"<!-- arcokf:inventory-sha256:([0-9a-f]{64}) -->")
_ENTRY_RE = re.compile(r"<!-- arcokf:entry:(\{.*\}) -->")
_OPERATIONAL_DIRS = frozenset(
    {".git", ".arc", ".codex", "audit", "audits", "config", "secrets", "credentials"}
)
_RESERVED_NAMES = frozenset({"index.md", "context.md", "log.md"})


class CollectionIndexError(ValueError):
    """Raised when an index entry cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class CollectionEntry:
    """One authorized document in a collection inventory."""

    path: str
    title: str
    digest: str
    summary: str = ""


@dataclass(frozen=True, slots=True)
class CollectionIndexValidation:
    """Structured result from syntax and optional on-disk verification."""

    valid: bool
    error: str = ""
    entries: tuple[CollectionEntry, ...] = ()


def _canonical_entry(entry: CollectionEntry) -> dict[str, str]:
    _validate_entry(entry)
    return {
        "digest": entry.digest,
        "path": entry.path,
        "summary": _one_line(entry.summary),
        "title": _one_line(entry.title),
    }


def _validate_entry(entry: CollectionEntry) -> None:
    path = PurePosixPath(entry.path)
    if not entry.path or path.is_absolute() or ".." in path.parts:
        raise CollectionIndexError(f"collection path is not relative: {entry.path!r}")
    if path.suffix.lower() != ".md" or path.name in _RESERVED_NAMES:
        raise CollectionIndexError(
            f"collection path is not an authorized OKF document: {entry.path!r}"
        )
    if any(part.lower() in _OPERATIONAL_DIRS for part in path.parts):
        raise CollectionIndexError(f"collection path is operational: {entry.path!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", entry.digest):
        raise CollectionIndexError(f"invalid SHA-256 digest for {entry.path!r}")
    if not entry.title.strip():
        raise CollectionIndexError(f"collection title is empty: {entry.path!r}")


def _one_line(value: str) -> str:
    return " ".join(str(value).split())


def _sorted_entries(
    entries: list[CollectionEntry] | tuple[CollectionEntry, ...],
) -> list[CollectionEntry]:
    result = list(entries)
    for entry in result:
        _validate_entry(entry)
    result.sort(key=lambda item: item.path)
    if len({entry.path for entry in result}) != len(result):
        raise CollectionIndexError("collection inventory contains duplicate paths")
    return result


def _inventory_digest(entries: list[CollectionEntry]) -> str:
    encoded = json.dumps(
        [_canonical_entry(entry) for entry in entries],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_collection_index(entries: list[CollectionEntry] | tuple[CollectionEntry, ...]) -> str:
    """Render a stable human-readable index with a canonical inventory digest."""
    ordered = _sorted_entries(entries)
    lines = [_HEADER, f"<!-- arcokf:inventory-sha256:{_inventory_digest(ordered)} -->", ""]
    for entry in ordered:
        canonical = _canonical_entry(entry)
        label = _one_line(entry.title).replace("[", "(").replace("]", ")")
        summary = _one_line(entry.summary)
        suffix = f" — {summary}" if summary else ""
        lines.append(f"- [{label}]({entry.path}) — sha256:{entry.digest}{suffix}")
        lines.append(
            "<!-- arcokf:entry:"
            + json.dumps(canonical, ensure_ascii=False, sort_keys=True)
            + " -->"
        )
    return "\n".join(lines) + "\n"


def _parse_index(text: str) -> tuple[CollectionEntry, ...]:
    lines = text.splitlines()
    if len(lines) < 5 or lines[:3] != _HEADER.splitlines():
        raise CollectionIndexError("invalid collection index header")
    inventory_match = _INVENTORY_RE.fullmatch(lines[3])
    if inventory_match is None:
        raise CollectionIndexError("missing collection inventory digest")
    del inventory_match  # checked again against canonical entries below
    entries: list[CollectionEntry] = []
    if lines[4] != "":
        raise CollectionIndexError("invalid collection index separator")
    for line in lines[5:]:
        if not line:
            continue
        if line.startswith("- ["):
            continue
        match = _ENTRY_RE.fullmatch(line)
        if match is None:
            raise CollectionIndexError("unexpected collection index content")
        try:
            raw = json.loads(match.group(1))
            entry = CollectionEntry(
                path=str(raw["path"]),
                title=str(raw["title"]),
                digest=str(raw["digest"]),
                summary=str(raw.get("summary", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CollectionIndexError("invalid collection index entry") from exc
        entries.append(entry)
    ordered = _sorted_entries(entries)
    expected = render_collection_index(ordered)
    if text != expected:
        raise CollectionIndexError("collection index is not canonical or was tampered")
    return tuple(ordered)


def validate_collection_index(index: Path, root: Path | None = None) -> CollectionIndexValidation:
    """Validate an index and, when supplied, every listed document digest."""
    try:
        text = index.read_text(encoding="utf-8")
        entries = _parse_index(text)
        if root is not None:
            root = root.resolve()
            expected = inventory_documents(root)
            if entries != expected:
                raise CollectionIndexError("collection index inventory is stale or tampered")
            for entry in entries:
                target = (root / PurePosixPath(entry.path)).resolve()
                if root not in target.parents or not target.is_file():
                    raise CollectionIndexError(f"indexed document is missing: {entry.path}")
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if digest != entry.digest:
                    raise CollectionIndexError(f"indexed document digest mismatch: {entry.path}")
                result = lint(target)
                if not result.valid:
                    raise CollectionIndexError(f"indexed document is invalid OKF: {entry.path}")
        return CollectionIndexValidation(True, entries=entries)
    except (CollectionIndexError, OSError, UnicodeError) as exc:
        return CollectionIndexValidation(False, error=str(exc))


def inventory_documents(root: Path) -> tuple[CollectionEntry, ...]:
    """Enumerate valid, authorized Markdown documents under ``root`` deterministically."""
    root = root.resolve()
    entries: list[CollectionEntry] = []
    for path in sorted(root.rglob("*.md")):
        resolved = path.resolve()
        if root not in resolved.parents:
            continue
        relative = path.relative_to(root).as_posix()
        if path.name in _RESERVED_NAMES or any(
            part.lower() in _OPERATIONAL_DIRS for part in PurePosixPath(relative).parts
        ):
            continue
        entry = document_entry(path, root)
        if entry is not None:
            entries.append(entry)
    return tuple(_sorted_entries(entries))


def document_entry(path: Path, root: Path) -> CollectionEntry | None:
    """Return one authorized unclassified document entry without walking siblings."""
    result = lint(path)
    if not result.valid or result.document is None:
        return None
    metadata = result.document.metadata
    # A shared index is intentionally safe for the lowest clearance only. Missing,
    # malformed, or elevated labels never enter it; a higher-clearance collection
    # can use a separate owner/index in a future extension.
    classification = str(metadata.get("classification") or "").strip().lower()
    if classification != "unclassified":
        return None
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    title = str(metadata.get("title") or metadata.get("name") or path.stem)
    body_lines = [line.strip() for line in result.document.body.splitlines() if line.strip()]
    summary = next((line.lstrip("#- ").strip() for line in body_lines), "")
    return CollectionEntry(
        relative,
        title,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        summary,
    )


__all__ = [
    "CollectionEntry",
    "CollectionIndexError",
    "CollectionIndexValidation",
    "document_entry",
    "inventory_documents",
    "render_collection_index",
    "render_index",
    "validate_collection_index",
    "validate_index",
]

# Short names make the contract convenient for stores while the explicit names
# remain the documented public API.
render_index = render_collection_index
validate_index = validate_collection_index
