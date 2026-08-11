"""Compatibility facade for neutral :mod:`arctrust` redaction primitives."""

from __future__ import annotations

from arctrust.redaction import (
    ALL_CATEGORIES,
    DEFAULT_OFF_ENTITIES,
    MAX_REGEX_SCAN_LENGTH,
    SECRETS_CATEGORY,
    PiiDetector,
    PiiMatch,
    RedactionConfigError,
    aba_checksum_valid,
    iban_mod97_valid,
    luhn_valid,
    redact_text,
)
from arctrust.redaction import EntityToggle as _EntityToggle
from arctrust.redaction import RegexPiiDetector as _RegexPiiDetector

from arcllm.exceptions import ArcLLMConfigError


class EntityToggle(_EntityToggle):
    """ArcLLM-compatible entity toggle using ArcLLM's config exception."""

    @classmethod
    def from_config(
        cls,
        config: dict[str, list[str]] | None,
        known_categories: frozenset[str] = ALL_CATEGORIES,
        default_off: frozenset[str] = DEFAULT_OFF_ENTITIES,
    ) -> EntityToggle:
        try:
            resolved = super().from_config(config, known_categories, default_off)
        except RedactionConfigError as exc:
            raise ArcLLMConfigError(str(exc)) from exc
        return cls(enabled=resolved.enabled)


class RegexPiiDetector(_RegexPiiDetector):
    """ArcLLM-compatible detector backed by neutral ArcTrust mechanics."""

    def __init__(
        self,
        custom_patterns: list[dict[str, str]] | None = None,
        entities: dict[str, list[str]] | None = None,
    ) -> None:
        try:
            super().__init__(custom_patterns=custom_patterns, entities=entities)
        except RedactionConfigError as exc:
            raise ArcLLMConfigError(str(exc)) from exc


__all__ = [
    "ALL_CATEGORIES",
    "DEFAULT_OFF_ENTITIES",
    "MAX_REGEX_SCAN_LENGTH",
    "SECRETS_CATEGORY",
    "EntityToggle",
    "PiiDetector",
    "PiiMatch",
    "RegexPiiDetector",
    "aba_checksum_valid",
    "iban_mod97_valid",
    "luhn_valid",
    "redact_text",
]
