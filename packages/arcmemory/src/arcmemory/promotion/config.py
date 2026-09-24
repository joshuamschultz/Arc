"""Promotion config — the operator-tunable score bands (SPEC-083 COMP-009).

Frozen: the router is pure and table-driven, so the bands it reads must not
change under it mid-decision. Off by default (``enabled=False``) — promotion is
opt-in per REQ-447.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PromotionConfig(BaseModel):
    """Score bands that decide auto / approve / never.

    ``auto_max`` is the highest score that promotes automatically; scores above
    it up to ``approve_max`` queue for operator approval; anything higher stays
    private. Defaults encode the 1-4 / 5-7 / 8-10 rubric anchors.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    auto_max: int = 4
    approve_max: int = 7


__all__ = ["PromotionConfig"]
