"""Promotion router — map an effective score to a decision (SPEC-083 COMP-004).

Pure and table-driven: given an item's effective privacy score and the config
bands, decide whether it promotes automatically, queues for operator approval,
or never leaves the agent's private scope. Fail-closed everywhere — an unscored
or out-of-range score is treated as private.
"""

from __future__ import annotations

from enum import StrEnum

from arcmemory.promotion.config import PromotionConfig


class PromotionDecision(StrEnum):
    """What may happen to a scored item.

    ``AUTO`` - promote immediately; ``APPROVE`` - queue for operator approval;
    ``NEVER`` - keep private, nothing leaves the agent's scope.
    """

    AUTO = "auto"
    APPROVE = "approve"
    NEVER = "never"


def route(effective_score: int | None, cfg: PromotionConfig) -> PromotionDecision:
    """Decide the promotion outcome for one item's effective score.

    Bands (defaults ``auto_max=4``, ``approve_max=7``):

    * ``None``                       -> NEVER (unscored, fail-closed);
    * ``<= auto_max``                -> AUTO;
    * ``auto_max < s <= approve_max`` -> APPROVE;
    * ``> approve_max``              -> NEVER.

    Defensive on out-of-range input: a score ``< 1`` (i.e. ``<= 0`` or negative)
    is not a real score and is treated as unscored -> NEVER; a score ``> 10`` is
    out of the 1-10 rubric range and also -> NEVER. Both fail closed rather than
    promote on a bogus value (e.g. a raw ``0`` would otherwise land in the AUTO
    band).
    """
    if effective_score is None:
        return PromotionDecision.NEVER
    if effective_score < 1 or effective_score > 10:
        return PromotionDecision.NEVER
    if effective_score <= cfg.auto_max:
        return PromotionDecision.AUTO
    if effective_score <= cfg.approve_max:
        return PromotionDecision.APPROVE
    return PromotionDecision.NEVER


__all__ = ["PromotionDecision", "route"]
