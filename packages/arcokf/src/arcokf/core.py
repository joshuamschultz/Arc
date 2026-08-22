"""Parsing and validation for the minimal OKF v0.2 document contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import Any, Protocol, cast


class _YAMLModule(Protocol):
    SafeLoader: type[Any]
    YAMLError: type[Exception]
    resolver: Any

    def safe_dump(self, data: Any, *, allow_unicode: bool, sort_keys: bool) -> str: ...


yaml = cast(_YAMLModule, import_module("yaml"))

MAX_DOCUMENT_BYTES = 2_000_000
MAX_FRONTMATTER_BYTES = 256_000
MAX_NESTING = 32
MAX_ALIASES = 16


class DiagnosticCode(StrEnum):
    INVALID_UTF8 = "invalid_utf8"
    INVALID_FRONTMATTER = "invalid_frontmatter"
    DUPLICATE_KEY = "duplicate_key"
    MISSING_TYPE = "missing_type"
    INVALID_TYPE = "invalid_type"
    DOCUMENT_TOO_LARGE = "document_too_large"
    FRONTMATTER_TOO_LARGE = "frontmatter_too_large"
    NESTING_TOO_DEEP = "nesting_too_deep"
    TOO_MANY_ALIASES = "too_many_aliases"
    WIKI_LINK = "wiki_link"
    RESERVED_DOCUMENT = "reserved_document"


@dataclass(frozen=True)
class Diagnostic:
    code: DiagnosticCode
    message: str
    path: str | None = None


@dataclass(frozen=True)
class Document:
    metadata: dict[str, Any]
    body: str
    path: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    diagnostics: tuple[Diagnostic, ...]
    document: Document | None = None


class OKFValidationError(ValueError):
    """Raised when input cannot be represented as a valid OKF document."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.message for item in diagnostics))


def _compose_node(self: Any, parent: Any, index: Any) -> Any:
    if self.check_event() and self.peek_event().__class__.__name__ == "AliasEvent":
        self.alias_count += 1
        if self.alias_count > MAX_ALIASES:
            raise ValueError("too many YAML aliases")
    return super(type(self), self).compose_node(parent, index)


_StrictLoader = type(
    "_StrictLoader", (yaml.SafeLoader,), {"alias_count": 0, "compose_node": _compose_node}
)


def _construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


cast(Any, _StrictLoader).add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _decode(source: str | bytes) -> tuple[str | None, Diagnostic | None]:
    if isinstance(source, bytes):
        try:
            return source.decode("utf-8"), None
        except UnicodeDecodeError:
            return None, Diagnostic(DiagnosticCode.INVALID_UTF8, "document is not valid UTF-8")
    return source, None


def _depth(value: Any, level: int = 0) -> int:
    if isinstance(value, dict):
        return max([level, *(_depth(item, level + 1) for item in value.values())])
    if isinstance(value, list):
        return max([level, *(_depth(item, level + 1) for item in value)])
    return level


def _frontmatter(text: str, path: str | None) -> tuple[dict[str, Any], str, list[Diagnostic]]:
    if not text.startswith("---\n"):
        return {}, text, []
    end = text.find("\n---", 4)
    if end < 0:
        return (
            {},
            text,
            [Diagnostic(DiagnosticCode.INVALID_FRONTMATTER, "frontmatter is not closed", path)],
        )
    raw = text[4:end]
    if len(raw.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        return (
            {},
            text,
            [Diagnostic(DiagnosticCode.FRONTMATTER_TOO_LARGE, "frontmatter is too large", path)],
        )
    try:
        loader = _StrictLoader(raw)
        try:
            metadata = loader.get_single_data()
        finally:
            loader.dispose()
    except ValueError as exc:
        code = (
            DiagnosticCode.DUPLICATE_KEY
            if "duplicate YAML key" in str(exc)
            else (
                DiagnosticCode.TOO_MANY_ALIASES
                if "too many YAML aliases" in str(exc)
                else DiagnosticCode.INVALID_FRONTMATTER
            )
        )
        return {}, text, [Diagnostic(code, str(exc), path)]
    except yaml.YAMLError as exc:
        return {}, text, [Diagnostic(DiagnosticCode.INVALID_FRONTMATTER, str(exc), path)]
    if not isinstance(metadata, dict):
        return (
            {},
            text,
            [
                Diagnostic(
                    DiagnosticCode.INVALID_FRONTMATTER, "frontmatter must be a mapping", path
                )
            ],
        )
    diagnostics: list[Diagnostic] = []
    if _depth(metadata) > MAX_NESTING:
        diagnostics.append(
            Diagnostic(DiagnosticCode.NESTING_TOO_DEEP, "frontmatter is nested too deeply", path)
        )
    return metadata, text[end + 4 :], diagnostics


def validate(source: str | bytes, *, path: str | None = None) -> ValidationResult:
    text, decoding_error = _decode(source)
    if decoding_error:
        return ValidationResult(False, (decoding_error,))
    if text is None:
        return ValidationResult(
            False, (Diagnostic(DiagnosticCode.INVALID_UTF8, "document is not valid UTF-8", path),)
        )
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        return ValidationResult(
            False, (Diagnostic(DiagnosticCode.DOCUMENT_TOO_LARGE, "document is too large", path),)
        )
    metadata, body, diagnostics = _frontmatter(text, path)
    basename = path.rsplit("/", 1)[-1] if path else None
    if basename in {"index.md", "log.md"}:
        if text.startswith("---\n"):
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.RESERVED_DOCUMENT,
                    f"{basename} cannot be a concept document",
                    path,
                )
            )
    elif "type" not in metadata:
        diagnostics.append(
            Diagnostic(DiagnosticCode.MISSING_TYPE, "concept requires a non-empty type", path)
        )
    elif not isinstance(metadata["type"], str):
        diagnostics.append(Diagnostic(DiagnosticCode.INVALID_TYPE, "type must be a string", path))
    elif not metadata["type"].strip():
        diagnostics.append(
            Diagnostic(DiagnosticCode.MISSING_TYPE, "concept requires a non-empty type", path)
        )
    if re.search(r"\[\[[^\]]+\]\]", body):
        diagnostics.append(
            Diagnostic(DiagnosticCode.WIKI_LINK, "wiki-links are not standard OKF links", path)
        )
    document = Document(metadata, body, path) if not diagnostics else None
    return ValidationResult(not diagnostics, tuple(diagnostics), document)


def parse(source: str | bytes, *, path: str | None = None) -> Document:
    result = validate(source, path=path)
    if not result.valid or result.document is None:
        raise OKFValidationError(result.diagnostics)
    return result.document


def render(document: Document) -> str:
    metadata = yaml.safe_dump(document.metadata, allow_unicode=True, sort_keys=True).rstrip()
    return f"---\n{metadata}\n---\n{document.body.lstrip()}"
