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

__all__ = [
    "VERSION",
    "Diagnostic",
    "DiagnosticCode",
    "Document",
    "OKFValidationError",
    "ValidationResult",
    "lint",
    "parse",
    "render",
    "validate",
]
