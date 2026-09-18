"""T-1122 (SPEC-083 COMP-009) — the memory module's ``promotion`` config block.

RED intent: the memory module config (``arcagent.modules.memory.config.MemoryConfig``)
must expose a nested ``promotion`` block with ``enabled: bool = False`` (default
OFF, REQ-447), ``auto_max: int = 4``, and ``approve_max: int = 7``, parseable from
a ``[modules.memory.config]``-shaped dict.

It fails today because:

- ``MemoryConfig().promotion`` raises ``AttributeError`` (no such field), and
- ``MemoryConfig(promotion={...})`` raises ``ValidationError`` — ``ModuleConfig``
  sets ``extra="forbid"``, so an undeclared ``promotion`` key is rejected.

Intended contract (for the GREEN implementer, T-1123): add a ``promotion`` field
whose value is a nested Pydantic model exposing ``enabled``/``auto_max``/
``approve_max`` with the defaults above, coercible from a plain dict so an
operator's TOML ``[modules.memory.config.promotion]`` table parses.
"""

from __future__ import annotations

from arcagent.modules.memory.config import MemoryConfig


def test_promotion_defaults_to_disabled_with_default_bands() -> None:
    """Out of the box the block is present, OFF, and carries the rubric bands."""
    cfg = MemoryConfig()

    assert cfg.promotion.enabled is False
    assert cfg.promotion.auto_max == 4
    assert cfg.promotion.approve_max == 7


def test_promotion_block_parses_from_a_config_dict() -> None:
    """A ``[modules.memory.config]``-shaped dict overrides every promotion field."""
    cfg = MemoryConfig(promotion={"enabled": True, "auto_max": 2, "approve_max": 5})

    assert cfg.promotion.enabled is True
    assert cfg.promotion.auto_max == 2
    assert cfg.promotion.approve_max == 5
