"""Narrow, per-type content extractors (COMP-005).

No all-in-one converter — a universal "sniff and convert anything" extractor
is an RCE surface (LLM03 supply chain, ASI05 unexpected code execution). Each
format gets its own small, sandboxed extractor. The dependency-free trio
(``TextExtractor``, ``MarkdownExtractor``, ``CodeExtractor``) is the default
path; the optional trio (``PdfExtractor``, ``DocxExtractor``, ``XlsxExtractor``)
lazy-imports its library *inside* ``extract()`` and raises
:class:`ExtractionUnavailable` instead of letting a bare ``ImportError``
escape — degrade, don't crash.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable


class ExtractionUnavailable(RuntimeError):  # noqa: N818 -- fixed contract name (COMP-005)
    """An optional extraction library (``arcmemory[docs]``) is not installed."""


@runtime_checkable
class Extractor(Protocol):
    """One format, one extractor. ``mimes`` are the mime types it claims."""

    mimes: tuple[str, ...]

    def extract(self, data: bytes, *, filename: str = "") -> str: ...


class _Utf8Extractor:
    """Base for the dependency-free text extractors: lenient UTF-8 decode, one place."""

    mimes: tuple[str, ...] = ()

    def extract(self, data: bytes, *, filename: str = "") -> str:
        return data.decode("utf-8", errors="replace")


class TextExtractor(_Utf8Extractor):
    """Dependency-free: ``text/plain``, ``.txt``, ``.log``."""

    mimes: tuple[str, ...] = ("text/plain",)


class MarkdownExtractor(_Utf8Extractor):
    """Dependency-free: ``text/markdown``, ``.md``."""

    mimes: tuple[str, ...] = ("text/markdown",)


class CodeExtractor(_Utf8Extractor):
    """Dependency-free: source code by extension — returned as-is (text)."""

    mimes: tuple[str, ...] = ()


class PdfExtractor:
    """``application/pdf`` via ``pypdf`` — text only, no JS/embedded-file execution."""

    mimes: tuple[str, ...] = ("application/pdf",)

    def extract(self, data: bytes, *, filename: str = "") -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ExtractionUnavailable("pypdf is not installed (arcmemory[docs])") from exc
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)


class DocxExtractor:
    """``.docx`` via ``python-docx`` — XML text only, no macro/OLE engine."""

    mimes: tuple[str, ...] = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    def extract(self, data: bytes, *, filename: str = "") -> str:
        try:
            import docx
        except ImportError as exc:
            raise ExtractionUnavailable("python-docx is not installed (arcmemory[docs])") from exc
        document = docx.Document(io.BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)


class XlsxExtractor:
    """``.xlsx`` via ``openpyxl`` (``read_only``, ``data_only``)."""

    mimes: tuple[str, ...] = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    def extract(self, data: bytes, *, filename: str = "") -> str:
        try:
            import openpyxl
        except ImportError as exc:
            raise ExtractionUnavailable("openpyxl is not installed (arcmemory[docs])") from exc
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines = [
            " ".join(str(cell) for cell in row if cell is not None)
            for sheet in workbook.worksheets
            for row in sheet.iter_rows(values_only=True)
        ]
        return "\n".join(lines)


_TEXT_EXTENSIONS = (".txt", ".log")
_MARKDOWN_EXTENSIONS = (".md", ".markdown")
_CODE_EXTENSIONS = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".rb",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".css",
    ".html",
    ".sql",
)

_MIME_EXTRACTORS: dict[str, Callable[[], Extractor]] = {
    "text/plain": TextExtractor,
    "text/markdown": MarkdownExtractor,
    "application/pdf": PdfExtractor,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocxExtractor,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": XlsxExtractor,
}


def get_extractor(mime: str = "", *, filename: str = "") -> Extractor | None:
    """Resolve by mime then filename extension. ``None`` when unsupported.

    An unsupported type is a caller-skip, never a crash — the caller audits
    the skip and moves on.
    """
    factory = _MIME_EXTRACTORS.get(mime)
    if factory is not None:
        return factory()
    ext = Path(filename).suffix.lower()
    if ext in _MARKDOWN_EXTENSIONS:
        return MarkdownExtractor()
    if ext in _TEXT_EXTENSIONS:
        return TextExtractor()
    if ext in _CODE_EXTENSIONS:
        return CodeExtractor()
    return None


__all__ = [
    "CodeExtractor",
    "DocxExtractor",
    "ExtractionUnavailable",
    "Extractor",
    "MarkdownExtractor",
    "PdfExtractor",
    "TextExtractor",
    "XlsxExtractor",
    "get_extractor",
]
