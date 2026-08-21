"""Proactive detected-moment recall config knobs (SPEC-071).

``MemoryConfig`` is frozen (Pydantic, ``ConfigDict(frozen=True)``) — every new
field must join that immutability contract, not sneak in as a mutable escape
hatch. These tests pin the two new tunables (``proactive_max_cards``,
``proactive_dedup_window``), their per-tier behavior, and that a
``dynamics`` override reaches a built brain's config the same way existing
knobs already do (mirrors ``test_provider.py``'s
``test_build_brain_dynamics_override_reaches_config``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from arcmemory import build_brain
from arcmemory.config import MemoryConfig


def test_proactive_max_cards_defaults_to_three() -> None:
    assert MemoryConfig().proactive_max_cards == 3


def test_proactive_dedup_window_has_a_positive_default() -> None:
    assert MemoryConfig().proactive_dedup_window > 0


def test_proactive_max_cards_is_frozen() -> None:
    cfg = MemoryConfig()
    assert cfg.proactive_max_cards == 3  # field must exist before the mutation attempt
    with pytest.raises(ValidationError):
        cfg.proactive_max_cards = 10  # type: ignore[misc]
    assert cfg.proactive_max_cards == 3  # rejected assignment must not have landed


def test_proactive_dedup_window_is_frozen() -> None:
    cfg = MemoryConfig()
    original = cfg.proactive_dedup_window  # field must exist before the mutation attempt
    with pytest.raises(ValidationError):
        cfg.proactive_dedup_window = 1  # type: ignore[misc]
    assert cfg.proactive_dedup_window == original  # rejected assignment must not have landed


def test_federal_tier_exposes_proactive_fields() -> None:
    cfg = MemoryConfig.for_tier("federal")
    assert isinstance(cfg.proactive_max_cards, int)
    assert isinstance(cfg.proactive_dedup_window, int)


def test_personal_tier_exposes_proactive_fields() -> None:
    cfg = MemoryConfig.for_tier("personal")
    assert isinstance(cfg.proactive_max_cards, int)
    assert isinstance(cfg.proactive_dedup_window, int)


def test_federal_tier_is_no_laxer_than_personal_on_proactive_cards() -> None:
    federal = MemoryConfig.for_tier("federal")
    personal = MemoryConfig.for_tier("personal")
    assert federal.proactive_max_cards <= personal.proactive_max_cards


def _context(tmp_path: Path, **backend: object) -> dict[str, Any]:
    return {
        "workspace": tmp_path,
        "agent_did": "did:arc:a",
        "tier": "personal",
        "audit_sink": None,
        "identity": None,
        "policy_pipeline": None,
        "backend_config": dict(backend),
    }


def test_build_brain_dynamics_override_reaches_proactive_max_cards(tmp_path: Path) -> None:
    """``backend_config['dynamics']`` overrides must reach the new proactive
    fields the same way they reach every other MemoryConfig knob — a brain
    built with an override that never lands is the producers-unwired trap."""
    brain = build_brain(
        _context(tmp_path, embed_backend="none", dynamics={"proactive_max_cards": 1})
    )

    assert brain._cfg.proactive_max_cards == 1  # type: ignore[attr-defined]
