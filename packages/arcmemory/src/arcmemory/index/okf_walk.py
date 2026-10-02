"""Retrieval by walking the OKF indexes: root index -> folder index -> documents.

This is the progressive-disclosure channel the per-folder ``index.md`` files
exist for. It reads the root listing, descends into the folders whose lines
best match the query (BM25 over the routing lines, deterministic, no model),
scores the documents those folders list, and returns the best ones as recalls
that fuse with the vector / BM25 / graph channels.

Trust and scope, in order:

* every index visited is verified (O(1) sidecar check) and a failing one is
  skipped, never read, so a tampered index degrades to ordinary recall;
* a document is opened only through a verified folder, must resolve inside
  ``memory/`` without a symlink, and must still match the digest the sidecar
  committed for it;
* the walk starts only at the surface index's own source folders, never follows
  a symlinked folder, and refuses anything that resolves under ``connected/`` at
  any depth: connected sources live behind per-source grants;
* bookkeeping cards are never returned (same list the surface index uses);
* every recall passes the no-read-up gate for the caller's clearance before it
  is returned, and a recall the gate drops is audited like any other.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from arcokf import IndexEntry, OKFValidationError, read_folder_digest
from arctrust.audit import AuditSink, NullSink
from arctrust.classification import Classification

from arcmemory.collection_index import MEMORY_NESTED_COLLECTIONS, memory_maintainer
from arcmemory.index.source import (
    BOOKKEEPING_ENTITY_TYPES,
    SOURCE_SUBDIRS,
    render_index_text,
)
from arcmemory.mdfile import parse_document
from arcmemory.security import gate_no_read_up
from arcmemory.types import Recall

_logger = logging.getLogger("arcmemory.okf_walk")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MAX_DEPTH = 4
#: Folders scanned when no folder line matches the query (the root lists only
#: names and counts, so most natural-language queries match none of them).
_FALLBACK_FOLDERS = 8
_BM25_K1 = 1.5
_BM25_B = 0.75


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _line_text(entry: IndexEntry) -> str:
    return f"{entry.title} {entry.description} {entry.path}"


def _bm25(query: Sequence[str], texts: Sequence[str]) -> list[float]:
    """Okapi BM25 of ``query`` against each text, using ``texts`` as the corpus."""
    docs = [Counter(_tokens(text)) for text in texts]
    if not docs:
        return []
    lengths = [sum(doc.values()) for doc in docs]
    average = (sum(lengths) / len(docs)) or 1.0
    scores = [0.0] * len(docs)
    for term in set(query):
        holding = sum(1 for doc in docs if term in doc)
        if not holding:
            continue
        idf = math.log(1 + (len(docs) - holding + 0.5) / (holding + 0.5))
        for i, doc in enumerate(docs):
            tf = doc.get(term, 0)
            if tf:
                norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * lengths[i] / average)
                scores[i] += idf * tf * (_BM25_K1 + 1) / (tf + norm)
    return scores


class OkfWalker:
    """Walk one memory directory's verified indexes for a query."""

    def __init__(
        self,
        mem_dir: Path,
        workspace: Path,
        *,
        clearance: Classification,
        strict: bool,
        actor_did: str,
        tier: str,
        top_folders: int = 3,
        top_docs: int = 10,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._mem_dir = Path(mem_dir)
        self._workspace = Path(workspace)
        self._clearance = clearance
        self._strict = strict
        self._actor_did = actor_did
        self._tier = tier
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._top_folders = top_folders
        self._top_docs = top_docs
        self._maintainer = memory_maintainer(self._mem_dir)

    def walk(self, query: str) -> list[Recall]:
        """The best documents the indexes route ``query`` to, gated on clearance.

        Blocking file I/O; callers on the event loop must run it in a thread.
        """
        terms = _tokens(query)
        if not terms:
            return []
        ranked = self._rank_documents(terms)
        recalls = [r for folder, entry, score in ranked if (r := self._open(folder, entry, score))]
        return gate_no_read_up(
            recalls,
            clearance=self._clearance,
            strict=self._strict,
            actor_did=self._actor_did,
            tier=self._tier,
            audit_sink=self._audit,
        )

    def _within_scope(self, path: Path) -> bool:
        """Whether ``path`` is inside ``memory/``, off ``connected/`` and symlink-free.

        Checked on the real path at every depth: a symlinked folder can alias a
        grant-gated connected source into an allowed folder, so the link itself
        is refused, and so is anything that resolves under ``connected/``.
        """
        try:
            relative = path.relative_to(self._mem_dir)
        except ValueError:
            return False
        walked = self._mem_dir
        for part in relative.parts:
            walked = walked / part
            if walked.is_symlink():
                return False
        resolved = path.resolve()
        mem = self._mem_dir.resolve()
        if not resolved.is_relative_to(mem):
            return False
        return not any(resolved.is_relative_to(mem / name) for name in _NESTED)

    def _entries(self, folder: Path) -> tuple[IndexEntry, ...]:
        if not self._within_scope(folder):
            _logger.warning("okf walk refused folder outside its scope: %s", folder)
            return ()
        validation = self._maintainer.validate(folder)
        if not validation.valid:
            _logger.warning("okf walk skipped unverified index %s: %s", folder, validation.error)
            return ()
        return validation.entries

    def _rank_documents(self, terms: list[str]) -> list[tuple[Path, IndexEntry, float]]:
        documents: list[tuple[Path, IndexEntry]] = []
        frontier = [self._mem_dir]
        for _ in range(_MAX_DEPTH):
            folders: list[tuple[Path, IndexEntry]] = []
            for folder in frontier:
                for entry in self._entries(folder):
                    if not entry.is_folder:
                        documents.append((folder, entry))
                    elif folder != self._mem_dir or _allowed_root(entry):
                        folders.append((folder, entry))
            frontier = self._choose_folders(terms, folders)
            if not frontier:
                break
        scores = _bm25(terms, [_line_text(entry) for _, entry in documents])
        hits = [(f, e, s) for (f, e), s in zip(documents, scores, strict=True) if s > 0]
        hits.sort(key=lambda hit: (-hit[2], hit[0].as_posix(), hit[1].path))
        return hits[: self._top_docs]

    def _choose_folders(
        self, terms: list[str], folders: list[tuple[Path, IndexEntry]]
    ) -> list[Path]:
        scores = _bm25(terms, [_line_text(entry) for _, entry in folders])
        matched = sorted(
            (
                (s, f / e.path.split("/", 1)[0])
                for (f, e), s in zip(folders, scores, strict=True)
                if s > 0
            ),
            key=lambda pair: (-pair[0], pair[1].as_posix()),
        )
        if matched:
            return [folder for _, folder in matched[: self._top_folders]]
        return [f / e.path.split("/", 1)[0] for f, e in folders[:_FALLBACK_FOLDERS]]

    def _open(self, folder: Path, entry: IndexEntry, score: float) -> Recall | None:
        """Read one indexed document, refusing anything the sidecar does not vouch for."""
        path = folder / entry.path
        digest = read_folder_digest(folder)
        try:
            if not self._within_scope(path):
                return None
            raw = path.read_bytes()
            if digest is None or hashlib.sha256(raw).hexdigest() != digest.docs.get(entry.path):
                _logger.warning("okf walk skipped changed document %s", path)
                return None
            metadata, body = parse_document(raw.decode("utf-8"))
        except (OSError, UnicodeError, OKFValidationError):
            return None
        if metadata.get("entity_type") in BOOKKEEPING_ENTITY_TYPES:
            return None
        return Recall(
            source=f"file:{path.relative_to(self._workspace).as_posix()}",
            content=render_index_text(metadata, body),
            score=score,
            kind="surface",
            classification=str(metadata.get("classification") or ""),
        )


_NESTED = tuple(sorted(MEMORY_NESTED_COLLECTIONS))


def _allowed_root(entry: IndexEntry) -> bool:
    """Only the surface index's own source folders are walked from the root."""
    return entry.path.split("/", 1)[0] in SOURCE_SUBDIRS


__all__ = ["OkfWalker"]
