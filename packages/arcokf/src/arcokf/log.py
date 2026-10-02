"""OKF reserved ``log.md``: a deterministic, newest-first change history.

A bundle root may carry a ``log.md`` listing what changed and when::

    # Log

    - 2026-10-02 **Creation** [Quarterly plan](plans/q4.md) - Draft for review.
    - 2026-10-01 **Update** [Roadmap](roadmap.md)
    - 2026-09-30 **Deprecation** [Old notes](old.md)

Like the folder index, the visible file stays spec-shaped (no frontmatter, one
line per change) and integrity lives in a ``.log.digest`` sidecar holding the
SHA-256 of the log and of every ``log.YYYY.md`` archive beside it. ``arcokf``
only renders, parses and validates; the owning store decides when to write.
Rendering is a pure function of the entries, so an unchanged history always
produces identical bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from .core import is_reserved_name
from .safe_io import read_regular_file

LOG_NAME = "log.md"
LOG_DIGEST_NAME = ".log.digest"
LOG_HEADING = "# Log"
CREATION = "Creation"
UPDATE = "Update"
DEPRECATION = "Deprecation"
KINDS = (CREATION, UPDATE, DEPRECATION)

_MAX_TITLE = 120
_MAX_SUMMARY = 200
_LINE_RE = re.compile(
    r"^- (\d{4}-\d{2}-\d{2}) \*\*(Creation|Update|Deprecation)\*\* "
    r"\[([^\]]*)\]\(([^)]*)\)(?: - (.*))?$"
)
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_YEAR_RE = re.compile(r"\d{4}")

# What one day's several changes to the same document collapse to: (earlier, later).
_COLLAPSE = {
    (CREATION, CREATION): CREATION,
    (CREATION, UPDATE): CREATION,
    (CREATION, DEPRECATION): DEPRECATION,
    (UPDATE, CREATION): UPDATE,
    (UPDATE, UPDATE): UPDATE,
    (UPDATE, DEPRECATION): DEPRECATION,
    (DEPRECATION, CREATION): UPDATE,
    (DEPRECATION, UPDATE): UPDATE,
    (DEPRECATION, DEPRECATION): DEPRECATION,
}


class ChangeLogError(ValueError):
    """Raised when a log entry or log text cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One change: ``kind`` happened to the document at ``path`` on ``day``."""

    day: str
    kind: str
    path: str
    title: str
    summary: str = ""


@dataclass(frozen=True, slots=True)
class LogDigest:
    """The parsed ``.log.digest`` sidecar: the log's hash and each archive's."""

    log: str
    archives: dict[str, str]


def archive_name(year: str) -> str:
    """The archive file name for ``year`` (``log.2026.md``)."""
    if not _YEAR_RE.fullmatch(year):
        raise ChangeLogError(f"invalid archive year: {year!r}")
    return f"log.{year}.md"


def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value).split())[:limit]


def _normalised(entry: LogEntry) -> LogEntry:
    """The entry exactly as it round-trips through its rendered line."""
    try:
        day = date.fromisoformat(entry.day).isoformat()
    except ValueError as exc:
        raise ChangeLogError(f"log date is not ISO: {entry.day!r}") from exc
    if entry.kind not in KINDS:
        raise ChangeLogError(f"unknown log kind: {entry.kind!r}")
    path = PurePosixPath(entry.path)
    if (
        not entry.path
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in entry.path
        or any(ord(ch) < 32 for ch in entry.path)
        or path.suffix.lower() != ".md"
        or is_reserved_name(path.name)
        or "//" in entry.path
    ):
        raise ChangeLogError(f"log path is not a relative document: {entry.path!r}")
    title = _one_line(entry.title, _MAX_TITLE).replace("[", "(").replace("]", ")")
    if not title:
        raise ChangeLogError(f"log title is empty: {entry.path!r}")
    return replace(
        entry,
        day=day,
        path=path.as_posix(),
        title=title,
        summary=_one_line(entry.summary, _MAX_SUMMARY),
    )


def _line(entry: LogEntry) -> str:
    tail = f" - {entry.summary}" if entry.summary else ""
    link = f"[{entry.title}]({quote(entry.path, safe='/')})"
    return f"- {entry.day} **{entry.kind}** {link}{tail}"


def canonical_order(entries: list[LogEntry] | tuple[LogEntry, ...]) -> list[LogEntry]:
    """Normalise ``entries`` and order them newest first, then by path."""
    ordered = sorted((_normalised(entry) for entry in entries), key=lambda e: (e.path, e.kind))
    ordered.sort(key=lambda e: e.day, reverse=True)
    return ordered


def render_change_log(entries: list[LogEntry] | tuple[LogEntry, ...]) -> str:
    """Render a log: a heading, then one line per change, newest first."""
    ordered = canonical_order(entries)
    if len({(e.day, e.path) for e in ordered}) != len(ordered):
        raise ChangeLogError("log holds two entries for one document on one day")
    body = "\n".join(_line(entry) for entry in ordered)
    return f"{LOG_HEADING}\n\n{body}\n" if body else f"{LOG_HEADING}\n"


def parse_change_log(text: str) -> tuple[LogEntry, ...]:
    """Parse log text back into entries; reject anything that is not canonical."""
    entries: list[LogEntry] = []
    lines = text.splitlines()
    if not lines or lines[0] != LOG_HEADING:
        raise ChangeLogError("log must start with the '# Log' heading and carry no frontmatter")
    for line in lines[1:]:
        if not line:
            continue
        match = _LINE_RE.match(line)
        if match is None:
            raise ChangeLogError("unexpected log content")
        day, kind, title, target, summary = match.groups()
        entries.append(LogEntry(day, kind, unquote(target), title, summary or ""))
    if render_change_log(entries) != text:
        raise ChangeLogError("log is not canonical or was tampered")
    return tuple(entries)


def merge_log_events(
    existing: list[LogEntry] | tuple[LogEntry, ...], events: list[LogEntry] | tuple[LogEntry, ...]
) -> list[LogEntry]:
    """Fold new ``events`` into ``existing`` with one entry per document per day.

    Several changes to one document on one day collapse to a single line (a
    creation followed by edits stays a creation; a later deletion wins). The
    result is in canonical order.
    """
    merged: dict[tuple[str, str], LogEntry] = {}
    for raw in (*existing, *events):
        entry = _normalised(raw)
        key = (entry.day, entry.path)
        prior = merged.get(key)
        kind = entry.kind if prior is None else _COLLAPSE[(prior.kind, entry.kind)]
        merged[key] = replace(entry, kind=kind)
    return canonical_order(list(merged.values()))


def render_log_digest(log_text: str, archives: dict[str, str]) -> str:
    """The sidecar for ``log_text``: its SHA-256 plus each archive's, by year."""
    payload = {
        "archives": dict(sorted(archives.items())),
        "log": hashlib.sha256(log_text.encode("utf-8")).hexdigest(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def parse_log_digest(text: str) -> LogDigest:
    """Parse a sidecar; any malformed shape is an error, never a partial trust."""
    try:
        raw = json.loads(text)
        log = str(raw["log"])
        archives = {str(k): str(v) for k, v in dict(raw["archives"]).items()}
    except (KeyError, TypeError, ValueError) as exc:
        raise ChangeLogError("invalid log digest sidecar") from exc
    if not _SHA_RE.fullmatch(log) or not all(
        _YEAR_RE.fullmatch(year) and _SHA_RE.fullmatch(sha) for year, sha in archives.items()
    ):
        raise ChangeLogError("invalid log digest sidecar")
    return LogDigest(log=log, archives=archives)


def read_log_digest(folder: Path) -> LogDigest | None:
    """The folder's log sidecar, or ``None`` when absent or unreadable."""
    try:
        return parse_log_digest(read_regular_file(folder / LOG_DIGEST_NAME).decode("utf-8"))
    except (OSError, UnicodeError, ChangeLogError):
        return None


def read_verified_log(
    folder: Path, name: str = LOG_NAME, *, digest: LogDigest | None = None
) -> tuple[LogEntry, ...] | None:
    """The entries of ``folder/name`` if it is canonical and matches its sidecar.

    ``name`` is ``log.md`` or an archive (``log.YYYY.md``). ``None`` means
    untrusted: absent, malformed, edited behind the owner's back or never
    recorded in the sidecar. A reader fails closed on ``None``.

    ``digest`` is a sidecar the caller already verified (an owner that signs it);
    without one the sidecar on disk is read. The log is read once, never through
    a symlink, and parsed from the bytes that were hashed.
    """
    if digest is None:
        digest = read_log_digest(folder)
    if digest is None:
        return None
    expected = digest.log if name == LOG_NAME else digest.archives.get(name.split(".")[1], "")
    try:
        raw = read_regular_file(folder / name)
        if hashlib.sha256(raw).hexdigest() != expected:
            return None
        return parse_change_log(raw.decode("utf-8"))
    except (OSError, UnicodeError, ChangeLogError):
        return None


__all__ = [
    "CREATION",
    "DEPRECATION",
    "KINDS",
    "LOG_DIGEST_NAME",
    "LOG_HEADING",
    "LOG_NAME",
    "UPDATE",
    "ChangeLogError",
    "LogDigest",
    "LogEntry",
    "archive_name",
    "canonical_order",
    "merge_log_events",
    "parse_change_log",
    "parse_log_digest",
    "read_log_digest",
    "read_verified_log",
    "render_change_log",
    "render_log_digest",
]
