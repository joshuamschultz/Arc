"""Strict, dependency-light Open Knowledge Format primitives."""

from .core import (
    VERSION,
    Diagnostic,
    DiagnosticCode,
    Document,
    OKFValidationError,
    ValidationResult,
    lint,
    parse,
    render,
    validate,
)
from .index import (
    CollectionEntry,
    CollectionIndexError,
    CollectionIndexValidation,
    document_entry,
    inventory_documents,
    render_collection_index,
    render_index,
    validate_collection_index,
    validate_index,
)

__all__ = [
    "VERSION",
    "CollectionEntry",
    "CollectionIndexError",
    "CollectionIndexValidation",
    "Diagnostic",
    "DiagnosticCode",
    "Document",
    "OKFValidationError",
    "ValidationResult",
    "document_entry",
    "inventory_documents",
    "lint",
    "parse",
    "render",
    "render_collection_index",
    "render_index",
    "validate",
    "validate_collection_index",
    "validate_index",
]
