"""T-1189 (SPEC-083 COMP-011) — the memory module's ``promotion`` config block.

Rewritten from the retired T-1122 band assertions (``auto_max``/``approve_max``).

RED intent: ``MemoryPromotionConfig`` still carries the retired bands and has no
threshold / pinned model / caps, and ``MemoryConfig`` has no federal validator.
These tests fail on schema mismatches and validators that do not reject yet —
not on imports (both classes exist today).

Contract under test (SDD COMP-011, README decisions 4, 5, 7):

- ``arcagent.modules.memory.config.MemoryPromotionConfig`` is the module-local
  mirror of ``arcmemory.promotion.config.PromotionConfig`` — same fields, same
  defaults, same validators — so the memory module still loads with arcmemory
  absent.
- ``MemoryConfig`` rejects ``promotion.enabled`` when ``tier == "federal"``.
- Personal and enterprise produce identical promotion defaults.
- ``[modules.memory.config.promotion]`` parses from TOML.
"""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from arcagent.modules.memory.config import MemoryConfig, MemoryPromotionConfig

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


def test_promotion_defaults_to_disabled_with_pinned_conservative_settings() -> None:
    """Out of the box the block is present, OFF, threshold 0.95, model pinned."""
    assert MemoryConfig().promotion.model_dump() == _EXPECTED_DEFAULTS


def test_promotion_band_fields_are_deleted() -> None:
    """The auto/approve band schema is gone — the mirror carries only COMP-011 fields."""
    assert set(MemoryPromotionConfig.model_fields) == set(_EXPECTED_DEFAULTS)


def test_mirror_matches_arcmemory_promotion_config() -> None:
    """The module-local mirror never drifts from the arcmemory source of truth."""
    arcmemory_config = pytest.importorskip("arcmemory.promotion.config")

    assert MemoryPromotionConfig().model_dump() == arcmemory_config.PromotionConfig().model_dump()


@pytest.mark.parametrize("threshold", [0.8999, 0.5, 1.0001])
def test_promotion_threshold_outside_floor_or_ceiling_is_rejected(threshold: float) -> None:
    with pytest.raises(ValidationError):
        MemoryConfig(promotion={"confidence_threshold": threshold})


@pytest.mark.parametrize("threshold", [0.90, 1.0])
def test_promotion_threshold_at_floor_and_ceiling_is_accepted(threshold: float) -> None:
    cfg = MemoryConfig(promotion={"confidence_threshold": threshold})

    assert cfg.promotion.confidence_threshold == threshold


@pytest.mark.parametrize("alias", ["jev-latest", "Jev-Latest"])
def test_promotion_classifier_model_latest_alias_is_rejected(alias: str) -> None:
    with pytest.raises(ValidationError):
        MemoryConfig(promotion={"classifier_model": alias})


def test_federal_tier_with_promotion_enabled_fails_validation() -> None:
    """Federal never promotes — config validation refuses to turn it on (decision 7)."""
    with pytest.raises(ValidationError):
        MemoryConfig(tier="federal", promotion={"enabled": True})


def test_federal_tier_with_promotion_disabled_is_valid() -> None:
    """The narrowness pair: federal with the block OFF still loads."""
    cfg = MemoryConfig(tier="federal", promotion={"enabled": False})

    assert cfg.promotion.enabled is False


@pytest.mark.parametrize("tier", ["personal", "enterprise"])
def test_personal_and_enterprise_may_enable_promotion(tier: str) -> None:
    cfg = MemoryConfig(tier=tier, promotion={"enabled": True})

    assert cfg.promotion.enabled is True


def test_personal_and_enterprise_have_identical_promotion_defaults() -> None:
    personal = MemoryConfig(tier="personal").promotion
    enterprise = MemoryConfig(tier="enterprise").promotion

    assert personal == enterprise
    assert personal.model_dump() == _EXPECTED_DEFAULTS


_TOML = """
[modules.memory.config]
tier = "enterprise"

[modules.memory.config.promotion]
enabled = true
confidence_threshold = 0.97
max_personal_probability = 0.05
max_items_per_sweep = 50
max_item_bytes = 8192
classifier = "jev"
classifier_model = "jev-1.14"
request_timeout_seconds = 5.0
"""


def test_promotion_block_parses_from_toml() -> None:
    """An operator's ``[modules.memory.config.promotion]`` table overrides every field."""
    raw = tomllib.loads(_TOML)["modules"]["memory"]["config"]

    cfg = MemoryConfig(**raw)

    assert cfg.promotion.model_dump() == {
        "enabled": True,
        "confidence_threshold": 0.97,
        "max_personal_probability": 0.05,
        "max_items_per_sweep": 50,
        "max_item_bytes": 8192,
        "classifier": "jev",
        "classifier_model": "jev-1.14",
        "request_timeout_seconds": 5.0,
        "api_key_env": "TYPESAFE_API_KEY",
        "vault_path": None,
    }


def test_federal_toml_with_promotion_enabled_fails_validation() -> None:
    raw = tomllib.loads(_TOML.replace('tier = "enterprise"', 'tier = "federal"'))
    config = raw["modules"]["memory"]["config"]

    with pytest.raises(ValidationError):
        MemoryConfig(**config)
