"""Sanitized failures raised by capability-import intake."""

from __future__ import annotations


class CapabilityImportError(Exception):
    """An untrusted capability source failed a mandatory intake gate."""


class CapabilityImportPathError(CapabilityImportError):
    """An archive or local-tree path is unsafe or ambiguous."""


class CapabilityImportLimitError(CapabilityImportError):
    """An archive exceeds a configured resource bound."""


class CapabilityImportLayoutError(CapabilityImportError):
    """A source tree does not match the alpha capability layout."""


class CapabilityImportSourceError(CapabilityImportError):
    """A source cannot be read as a supported, regular input."""
