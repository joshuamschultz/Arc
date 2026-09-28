"""T-1189 (SPEC-083 COMP-011) — arcmemory ``PromotionConfig`` + the federal tier lock.

RED intent: ``arcmemory.promotion.config`` still holds the retired band model
(``auto_max``/``approve_max``); it has no threshold, pinned model, ``for_tier`` or
``PromotionForbiddenAtTierError``. The tests fail on missing fields / symbols and
on validators that do not reject yet.

Contract under test (SDD COMP-011, README decisions 4, 5, 7):

- frozen ``PromotionConfig`` with ``enabled=False``, ``confidence_threshold=0.95``
  (``0.90 <= x <= 1.0``), ``max_personal_probability=0.10``,
  ``max_items_per_sweep=200``, ``max_item_bytes=16_384``, ``classifier="jev"``,
  ``classifier_model="jev-1.13"`` (pinned; ``*-latest`` aliases rejected),
  ``request_timeout_seconds=10.0``. Band fields are gone.
- ``PromotionConfig.for_tier(tier, **operator)`` raises
  ``PromotionForbiddenAtTierError`` when ``tier == "federal"`` and ``enabled``.
- personal and enterprise produce identical defaults.

The module (which exists) is imported, and the new symbols are looked up inside
each test, so one missing symbol fails its own tests instead of the whole file.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcmemory.promotion import config as promotion_config

_EXPECTED_DEFAULTS = {
    "enabled": False,
    "confidence_threshold": 0.95,
    "max_personal_probability": 0.10,
    "max_items_per_sweep": 200,
    "max_item_bytes": 16_384,
    "classifier": "jev",
    "classifier_model": "jev-1.13",
    "request_timeout_seconds": 10.0,
    "api_key_env": "TYPESAFE_API_KEY",
    "vault_path": None,
}


def _cfg(**kwargs: object) -> promotion_config.PromotionConfig:
    return promotion_config.PromotionConfig(**kwargs)


def test_promotion_config_defaults_are_off_and_conservative() -> None:
    """Out of the box promotion is OFF, threshold 0.95, model pinned to jev-1.13."""
    assert _cfg().model_dump() == _EXPECTED_DEFAULTS


def test_promotion_config_band_fields_are_deleted() -> None:
    """The retired score bands are gone from the schema (no legacy shims)."""
    fields = set(promotion_config.PromotionConfig.model_fields)

    assert fields == set(_EXPECTED_DEFAULTS)


@pytest.mark.parametrize("threshold", [0.90, 0.95, 0.975, 1.0])
def test_confidence_threshold_inside_floor_and_ceiling_is_accepted(threshold: float) -> None:
    assert _cfg(confidence_threshold=threshold).confidence_threshold == threshold


@pytest.mark.parametrize("threshold", [0.0, 0.5, 0.8999, 1.0001, 2.0])
def test_confidence_threshold_outside_floor_or_ceiling_is_rejected(threshold: float) -> None:
    """Below 0.90 Jev is ~53% accurate in the mid band; above 1.0 is meaningless."""
    with pytest.raises(ValidationError):
        _cfg(confidence_threshold=threshold)


def test_confidence_threshold_nan_is_rejected() -> None:
    """NaN compares False against everything — it must not slip past the floor."""
    with pytest.raises(ValidationError):
        _cfg(confidence_threshold=float("nan"))


@pytest.mark.parametrize("alias", ["jev-latest", "Jev-Latest", "JEV-LATEST", "other-latest"])
def test_classifier_model_latest_alias_is_rejected(alias: str) -> None:
    """A floating alias defeats the version pin recorded on every decision."""
    with pytest.raises(ValidationError):
        _cfg(classifier_model=alias)


def test_classifier_model_empty_is_rejected() -> None:
    """An empty model string is not a pin."""
    with pytest.raises(ValidationError):
        _cfg(classifier_model="")


def test_classifier_model_explicit_pin_is_accepted() -> None:
    assert _cfg(classifier_model="jev-1.14").classifier_model == "jev-1.14"


def test_promotion_config_is_frozen() -> None:
    """The decision rule reads it mid-sweep; it must not change under it."""
    cfg = _cfg()

    with pytest.raises(ValidationError):
        cfg.confidence_threshold = 0.5  # type: ignore[misc]  # asserting frozen refusal


def test_for_tier_federal_enabled_raises_forbidden() -> None:
    """Federal never promotes — enforced in code, not by default (decision 7)."""
    forbidden = promotion_config.PromotionForbiddenAtTierError

    with pytest.raises(forbidden):
        promotion_config.PromotionConfig.for_tier("federal", enabled=True)


def test_for_tier_federal_disabled_is_allowed_and_off() -> None:
    """A federal agent may carry the (disabled) block; it just can never turn on."""
    cfg = promotion_config.PromotionConfig.for_tier("federal")

    assert cfg.enabled is False


def test_forbidden_at_tier_error_is_an_exception_type() -> None:
    assert issubclass(promotion_config.PromotionForbiddenAtTierError, Exception)


def test_for_tier_personal_and_enterprise_share_defaults() -> None:
    """Personal = enterprise (decision 7): same defaults, same behavior."""
    personal = promotion_config.PromotionConfig.for_tier("personal")
    enterprise = promotion_config.PromotionConfig.for_tier("enterprise")

    assert personal == enterprise
    assert personal.model_dump() == _EXPECTED_DEFAULTS


def test_for_tier_applies_operator_overrides() -> None:
    cfg = promotion_config.PromotionConfig.for_tier(
        "enterprise", enabled=True, confidence_threshold=0.97, classifier_model="jev-1.14"
    )

    assert cfg.enabled is True
    assert cfg.confidence_threshold == 0.97
    assert cfg.classifier_model == "jev-1.14"


def test_for_tier_still_validates_operator_overrides() -> None:
    """``for_tier`` is not a side door around the field validators."""
    with pytest.raises(ValidationError):
        promotion_config.PromotionConfig.for_tier("personal", confidence_threshold=0.5)
