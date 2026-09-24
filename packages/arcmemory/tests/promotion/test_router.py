"""T-1116 (SPEC-083 COMP-004) — the promotion router.

RED intent: ``arcmemory.promotion`` does not exist yet, so importing the router
and its config fails with ``ModuleNotFoundError`` — the feature is absent.

Intended contract (for the GREEN implementer):

- ``arcmemory.promotion.router.PromotionDecision`` — a ``StrEnum`` with members
  ``AUTO`` (``"auto"``), ``APPROVE`` (``"approve"``), ``NEVER`` (``"never"``).
- ``arcmemory.promotion.router.route(effective_score: int | None, cfg) -> PromotionDecision``
  where ``cfg`` exposes ``auto_max`` (default 4) and ``approve_max`` (default 7):
    * ``effective_score is None``        -> ``NEVER`` (fail-closed)
    * ``effective_score <= cfg.auto_max``    -> ``AUTO``
    * ``cfg.auto_max < score <= cfg.approve_max`` -> ``APPROVE``
    * ``score > cfg.approve_max``         -> ``NEVER``
- ``arcmemory.promotion.config.PromotionConfig`` — a (frozen) Pydantic model with
  ``enabled: bool = False``, ``auto_max: int = 4``, ``approve_max: int = 7``.

The router is pure, table-driven, and fail-closed.
"""

from __future__ import annotations

import pytest

from arcmemory.promotion.config import PromotionConfig
from arcmemory.promotion.router import PromotionDecision, route


@pytest.mark.parametrize("score", [1, 2, 3, 4])
def test_route_auto_band_promotes_automatically(score: int) -> None:
    """A score at/below ``auto_max`` (default 4) auto-promotes."""
    assert route(score, PromotionConfig()) is PromotionDecision.AUTO


@pytest.mark.parametrize("score", [5, 6, 7])
def test_route_approve_band_requires_operator(score: int) -> None:
    """A score in the middle band (default 5-7) queues for approval."""
    assert route(score, PromotionConfig()) is PromotionDecision.APPROVE


@pytest.mark.parametrize("score", [8, 9, 10])
def test_route_never_band_keeps_private(score: int) -> None:
    """A score at/above ``never_min`` (default 8) never promotes."""
    assert route(score, PromotionConfig()) is PromotionDecision.NEVER


def test_route_unscored_is_never_fail_closed() -> None:
    """An unscored item (``None``) is treated as never — fail-closed."""
    assert route(None, PromotionConfig()) is PromotionDecision.NEVER


def test_custom_thresholds_move_the_bands() -> None:
    """Operator config narrows the bands (auto_max=2, approve_max=5)."""
    cfg = PromotionConfig(auto_max=2, approve_max=5)

    assert route(2, cfg) is PromotionDecision.AUTO
    # 3 was AUTO under the default bands; the tighter auto_max drops it to APPROVE.
    assert route(3, cfg) is PromotionDecision.APPROVE
    assert route(5, cfg) is PromotionDecision.APPROVE
    # 6 was APPROVE under the default bands; the tighter approve_max pushes it to NEVER.
    assert route(6, cfg) is PromotionDecision.NEVER
