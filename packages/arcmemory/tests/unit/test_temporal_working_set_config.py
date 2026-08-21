"""RED — arcmemory temporal + working-set config knobs (SPEC-072 COMP-012).

``MemoryConfig`` is frozen; every new tunable joins that immutability contract. These
pin the working-set bound/decay + on/off and the temporal on/off, their defaults, the
per-tier variants, and that a disabled toggle is representable.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcmemory.config import MemoryConfig


def test_temporal_enabled_defaults_on() -> None:
    assert MemoryConfig().temporal_enabled is True


def test_working_set_enabled_defaults_on() -> None:
    assert MemoryConfig().working_set_enabled is True


def test_working_set_bound_and_decay_have_positive_defaults() -> None:
    cfg = MemoryConfig()
    assert cfg.working_set_max > 0
    assert cfg.working_set_decay_turns > 0


def test_toggles_are_representable_disabled() -> None:
    cfg = MemoryConfig(temporal_enabled=False, working_set_enabled=False)
    assert cfg.temporal_enabled is False
    assert cfg.working_set_enabled is False


def test_new_knobs_are_frozen() -> None:
    cfg = MemoryConfig()
    with pytest.raises(ValidationError):
        cfg.temporal_enabled = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        cfg.working_set_max = 1  # type: ignore[misc]
    assert cfg.temporal_enabled is True


def test_per_tier_variants_expose_new_knobs() -> None:
    for tier in ("personal", "enterprise", "federal"):
        cfg = MemoryConfig.for_tier(tier)  # type: ignore[arg-type]
        assert isinstance(cfg.temporal_enabled, bool)
        assert isinstance(cfg.working_set_enabled, bool)
        assert cfg.working_set_max > 0
        assert cfg.working_set_decay_turns > 0
