"""Promotion config + the federal tier lock (SPEC-083 COMP-011).

Frozen: the decision rule reads it mid-sweep, so it must not change under it.
Off by default (``enabled=False``) — promotion is opt-in (REQ-447). Personal and
enterprise share one set of defaults; federal can never turn promotion on.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The tier at which promotion is forbidden in code (decision 7).
_FEDERAL = "federal"


class PromotionForbiddenAtTierError(ValueError):
    """Raised when promotion is enabled at a tier that must never promote."""


def is_federal_tier(tier: str) -> bool:
    """True for the federal tier, whatever its case or surrounding whitespace."""
    return tier.strip().casefold() == _FEDERAL


class PromotionConfig(BaseModel):
    """Operator-tunable promotion settings.

    ``confidence_threshold`` has a 0.90 floor: below it the classifier's mid band
    is overconfident. ``classifier_model`` is a pinned version recorded on every
    decision; a floating ``*-latest`` alias would defeat that pin.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    confidence_threshold: float = Field(default=0.95, ge=0.90, le=1.0, allow_inf_nan=False)
    max_personal_probability: float = Field(default=0.10, ge=0.0, le=1.0, allow_inf_nan=False)
    max_items_per_sweep: int = Field(default=200, gt=0)
    max_item_bytes: int = Field(default=16_384, gt=0)
    classifier: str = Field(default="jev", min_length=1)
    classifier_model: str = "jev-1.13.0"
    request_timeout_seconds: float = Field(default=10.0, gt=0.0, allow_inf_nan=False)
    # Where the classifier key comes from — a coordinate, never the value (REQ-510).
    # One fleet-wide env var in the write-only key store (decision 12); an optional
    # vault path is tried first by the classifier's VaultResolver.
    api_key_env: str = Field(default="TYPESAFE_API_KEY", min_length=1)
    vault_path: str | None = None

    @field_validator("classifier_model")
    @classmethod
    def _require_pinned_model(cls, value: str) -> str:
        return _check_pinned_model(value)

    @classmethod
    def for_tier(cls, tier: str, **operator: Any) -> PromotionConfig:
        """Build the config for ``tier`` with the operator's overrides applied.

        Raises :class:`PromotionForbiddenAtTierError` when ``tier`` is federal and
        the result is enabled. Field validators still run on every override.
        """
        config = cls(**operator)
        if config.enabled and is_federal_tier(tier):
            raise PromotionForbiddenAtTierError(
                "memory promotion is forbidden at the federal tier"
            )
        return config


def _check_pinned_model(value: str) -> str:
    """Reject an empty model name or a floating ``latest`` alias."""
    folded = value.strip().casefold()
    if not folded:
        raise ValueError("classifier_model must name a pinned model version")
    if folded == "latest" or folded.endswith("-latest"):
        raise ValueError("classifier_model must be pinned; '*-latest' aliases are refused")
    return value


__all__ = [
    "PromotionConfig",
    "PromotionForbiddenAtTierError",
    "is_federal_tier",
]
