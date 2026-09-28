"""The promote / keep_private decision rule (SPEC-083 COMP-017).

Pure and conservative. Promote only when every check holds; anything else —
including a malformed or forged verdict built without model validation — keeps
the item private. This is the last gate before classifier output crosses DID
isolation, so it re-checks every field rather than trusting the verdict model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal

from arcmemory.promotion.classifier import PROMOTION_LABELS, ClassifierVerdict
from arcmemory.promotion.config import PromotionConfig

Decision = Literal["promote", "keep_private"]

#: Allowed drift of the probability sum from 1.0 (wire float rounding).
_SUM_TOLERANCE = 0.01


def decide(verdict: ClassifierVerdict, cfg: PromotionConfig) -> Decision:
    """Return ``"promote"`` only for a well-formed, confident ``company`` verdict."""
    try:
        promotable = _is_promotable(verdict, cfg)
    except (AttributeError, TypeError, ValueError):
        # A verdict missing fields or carrying wrong types is malformed: fail closed.
        promotable = False
    return "promote" if promotable else "keep_private"


def _is_promotable(verdict: ClassifierVerdict, cfg: PromotionConfig) -> bool:
    personal = verdict.personal_probability
    return (
        verdict.label == "company"
        and verdict.classifier_version == cfg.classifier_model
        and _is_probability(verdict.confidence)
        and verdict.confidence >= cfg.confidence_threshold
        and _company_leads(verdict.probabilities)
        and (
            personal is None
            or (_is_probability(personal) and personal <= cfg.max_personal_probability)
        )
    )


def _is_probability(value: object) -> bool:
    """A real, finite number in ``[0, 1]`` (``bool`` is not a probability)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0.0 <= value <= 1.0


def _company_leads(probabilities: object) -> bool:
    """Exactly the four labels, all valid, summing to 1.0, with ``company`` strictly highest."""
    if not isinstance(probabilities, Mapping) or set(probabilities) != set(PROMOTION_LABELS):
        return False
    values = [probabilities[label] for label in PROMOTION_LABELS]
    if not all(_is_probability(value) for value in values):
        return False
    if abs(math.fsum(values) - 1.0) > _SUM_TOLERANCE:
        return False
    company = probabilities["company"]
    return all(company > probabilities[label] for label in PROMOTION_LABELS if label != "company")


__all__ = ["Decision", "decide"]
