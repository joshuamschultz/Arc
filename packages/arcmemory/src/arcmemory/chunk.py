"""Recursive and AST-aware chunkers (COMP-005).

``RecursiveChunker`` splits :func:`arcmemory.security.document_sanitize`-cleaned
text into ~token-budget windows with an overlap fraction between adjacent
windows — sanitizing *before* splitting means an injection span dropped by
``document_sanitize`` never survives into any chunk. ``CodeChunker`` aligns
chunks to Python's own ``def``/``class`` boundaries via the stdlib ``ast``
module (no tree-sitter dependency) and falls back to ``RecursiveChunker`` for
non-Python or unparseable code — it never raises.
"""

from __future__ import annotations

import ast
from typing import Protocol, runtime_checkable

from arctrust.audit import AuditSink

from arcmemory.index.source import SourceChunk
from arcmemory.security import document_sanitize, token_estimate


@runtime_checkable
class Chunker(Protocol):
    """Split raw text into ``SourceChunk`` windows for a given source path."""

    def chunk(
        self,
        text: str,
        *,
        source_path: str,
        classification: str = "unclassified",
        mtime: float | None = None,
    ) -> list[SourceChunk]: ...


def _split_units(text: str, *, chunk_tokens: int) -> list[str]:
    """Paragraph units; a paragraph far over budget is split by line instead."""
    units: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if token_estimate(para) > chunk_tokens * 2:
            units.extend(line.strip() for line in para.split("\n") if line.strip())
        else:
            units.append(para)
    return units


def _overlap_tail(units: list[str], *, chunk_tokens: int, overlap: float) -> list[str]:
    """Trailing units worth ~``overlap`` fraction of ``chunk_tokens``, from the end."""
    budget = chunk_tokens * overlap
    if budget <= 0:
        return []
    tail: list[str] = []
    total = 0
    for unit in reversed(units):
        cost = token_estimate(unit)
        if tail and total + cost > budget:
            break
        tail.insert(0, unit)
        total += cost
    return tail


def _window_units(units: list[str], *, chunk_tokens: int, overlap: float) -> list[str]:
    """Greedily group units into ~``chunk_tokens`` windows, each starting with
    the overlap tail of the previous window."""
    if not units:
        return []
    windows: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = token_estimate(unit)
        if current and current_tokens + unit_tokens > chunk_tokens:
            windows.append("\n\n".join(current))
            current = _overlap_tail(current, chunk_tokens=chunk_tokens, overlap=overlap)
            current_tokens = sum(token_estimate(u) for u in current)
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        windows.append("\n\n".join(current))
    return windows


class RecursiveChunker:
    """Sanitize, then split into ~``chunk_tokens`` windows with ``overlap``."""

    def __init__(
        self,
        *,
        chunk_tokens: int = 512,
        overlap: float = 0.10,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._chunk_tokens = chunk_tokens
        self._overlap = overlap
        self._audit_sink = audit_sink

    def chunk(
        self,
        text: str,
        *,
        source_path: str,
        classification: str = "unclassified",
        mtime: float | None = None,
    ) -> list[SourceChunk]:
        sanitized = document_sanitize(text, max_length=None, audit_sink=self._audit_sink)
        units = _split_units(sanitized, chunk_tokens=self._chunk_tokens)
        windows = _window_units(units, chunk_tokens=self._chunk_tokens, overlap=self._overlap)
        resolved_mtime = 0.0 if mtime is None else mtime
        return [
            SourceChunk(
                chunk_id=f"{source_path}#{i}",
                source_path=source_path,
                text=window,
                classification=classification,
                mtime=resolved_mtime,
            )
            for i, window in enumerate(windows)
        ]


class CodeChunker:
    """One chunk per top-level Python ``def``/``class``; else recursive fallback."""

    def __init__(
        self,
        *,
        chunk_tokens: int = 512,
        overlap: float = 0.10,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._fallback = RecursiveChunker(
            chunk_tokens=chunk_tokens, overlap=overlap, audit_sink=audit_sink
        )

    def chunk(
        self,
        text: str,
        *,
        source_path: str,
        classification: str = "unclassified",
        mtime: float | None = None,
    ) -> list[SourceChunk]:
        if not source_path.endswith(".py"):
            return self._fallback.chunk(
                text, source_path=source_path, classification=classification, mtime=mtime
            )
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return self._fallback.chunk(
                text, source_path=source_path, classification=classification, mtime=mtime
            )
        nodes = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        ]
        if not nodes:
            return self._fallback.chunk(
                text, source_path=source_path, classification=classification, mtime=mtime
            )
        lines = text.splitlines()
        resolved_mtime = 0.0 if mtime is None else mtime
        chunks: list[SourceChunk] = []
        for i, node in enumerate(nodes):
            start = node.lineno - 1
            end = node.end_lineno if node.end_lineno is not None else node.lineno
            snippet = "\n".join(lines[start:end])
            chunks.append(
                SourceChunk(
                    chunk_id=f"{source_path}#{i}",
                    source_path=source_path,
                    text=snippet,
                    classification=classification,
                    mtime=resolved_mtime,
                )
            )
        return chunks


__all__ = ["Chunker", "CodeChunker", "RecursiveChunker"]
