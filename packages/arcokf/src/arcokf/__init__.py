"""Strict, dependency-light Open Knowledge Format primitives."""

from .core import (
    Diagnostic,
    DiagnosticCode,
    Document,
    OKFValidationError,
    ValidationResult,
    parse,
    render,
    validate,
)

__all__ = [
    "Diagnostic",
    "DiagnosticCode",
    "Document",
    "OKFValidationError",
    "ValidationResult",
    "parse",
    "render",
    "validate",
]
