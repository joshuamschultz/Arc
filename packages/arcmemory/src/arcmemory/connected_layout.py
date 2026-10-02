"""Where a connected document lives under its source's folder.

A connected document mirrors the source path it came from, so the per-folder
OKF indexes read like the source's own folder tree::

    memory/connected/<source_id>/Projects/Q4/plan-3fa91c2d7e01.md

The path is derived from ``SourceObject.locator``, which a remote system
controls, so it is untrusted input: traversal-shaped locators are refused,
every segment is reduced to a safe character set, and the file name carries a
short hash of the object id so two objects can never claim one path. A locator
with no usable path falls back to the flat ``<sha256(object_id)>.md`` name.

Pure functions only; the connected-data service owns every write.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from arcokf import DIGEST_NAME, INDEX_NAME, listable_dir

from arcmemory.mdfile import read_frontmatter

#: Folder segments beyond this depth fold into the file name, so a hostile
#: locator cannot make an unbounded directory chain.
MAX_FOLDER_DEPTH = 10
_MAX_SEGMENT = 80
_HASH_CHARS = 12
_UNSAFE = re.compile(r"[^\w.\- ]", re.UNICODE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


class LocatorRefusedError(ValueError):
    """The locator is shaped like a path traversal and may not name a location."""


def flat_name(object_id: str) -> str:
    """The legacy flat file name: the SHA-256 of the object id."""
    return hashlib.sha256(object_id.encode("utf-8")).hexdigest() + ".md"


def _segment(raw: str) -> str:
    """One path segment reduced to a safe, listable, non-hidden folder name."""
    clean = " ".join(_UNSAFE.sub("_", raw).split()).strip(" .")
    clean = clean.lstrip(".") or "_"
    clean = clean[:_MAX_SEGMENT].rstrip(" .") or "_"
    return clean if listable_dir(clean) else f"{clean}_"


def _split(locator: str) -> list[str]:
    """The path segments of ``locator``; refuse anything shaped like traversal."""
    if "\\" in locator or any(ord(ch) < 32 for ch in locator):
        raise LocatorRefusedError("locator carries a backslash or control character")
    text = unquote(locator)
    if "\\" in text or any(ord(ch) < 32 for ch in text):
        raise LocatorRefusedError("locator carries an encoded backslash or control character")
    if _SCHEME.match(text):
        parts = urlsplit(text)
        text = f"{parts.netloc}/{parts.path}"
    segments = [part for part in text.split("/") if part not in ("", ".")]
    if any(part.strip() == ".." for part in segments):
        raise LocatorRefusedError("locator contains a parent-directory segment")
    return segments


def mirror_relpath(locator: str, object_id: str) -> str | None:
    """The source-relative path ``locator`` mirrors, or ``None`` for the flat name.

    Raises :class:`LocatorRefusedError` for a traversal-shaped locator; the caller
    decides what refusal means (the service audits it and keeps the flat name).
    """
    segments = _split(locator)
    if not segments:
        return None
    *folders, leaf = segments
    folders = [_segment(part) for part in folders]
    if len(folders) > MAX_FOLDER_DEPTH:
        leaf = "-".join([*folders[MAX_FOLDER_DEPTH:], leaf])
        folders = folders[:MAX_FOLDER_DEPTH]
    stem = _segment(PurePosixPath(leaf).stem or leaf)[:_MAX_SEGMENT]
    suffix = hashlib.sha256(object_id.encode("utf-8")).hexdigest()[:_HASH_CHARS]
    return "/".join([*folders, f"{stem}-{suffix}.md"])


def contained(root: Path, relative: str) -> Path:
    """``root / relative``, proven to stay inside ``root`` (defence in depth)."""
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise LocatorRefusedError("mirrored path escapes the source root")
    return root / relative


def _owned_by_another(path: Path, object_id: str) -> bool:
    if not path.exists():
        return False
    try:
        owner = (read_frontmatter(path) or {}).get("external_id")
    except ValueError:
        return True  # an unreadable file is not ours to overwrite
    return owner != object_id


def target_path(
    root: Path, object_id: str, locator: str, *, current: Path | None = None
) -> tuple[Path, bool]:
    """Where one connected document belongs, and whether its locator was refused.

    ``current`` is the path the object already occupies, which is never a
    collision with itself. A mirrored path already held by a different object
    falls back to the flat per-object name, which is unique by construction.
    """
    refused = False
    relative: str | None = None
    if locator:
        try:
            relative = mirror_relpath(locator, object_id)
        except LocatorRefusedError:
            refused = True
    path = contained(root, relative or flat_name(object_id))
    if relative is not None and path != current and _owned_by_another(path, object_id):
        path = contained(root, flat_name(object_id))
    return path, refused


def prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove emptied folders from ``start`` upward, never ``stop`` or above it.

    A folder holding only its own derived index files counts as empty: those
    describe documents that are gone, and the parent's index drops the folder
    on its next regeneration.
    """
    folder = start
    while folder != stop and stop in folder.parents:
        try:
            names = {item.name for item in folder.iterdir()}
            if not names <= {INDEX_NAME, DIGEST_NAME}:
                return
            for name in names:
                (folder / name).unlink()
            folder.rmdir()
        except OSError:
            return  # already gone or racing a writer: the walk ends here
        folder = folder.parent


__all__ = [
    "MAX_FOLDER_DEPTH",
    "LocatorRefusedError",
    "contained",
    "flat_name",
    "mirror_relpath",
    "prune_empty_dirs",
    "target_path",
]
