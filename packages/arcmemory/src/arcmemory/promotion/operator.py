"""Operator decisions on promotable cards (alpha-2 item 16).

Two operator decisions join the classifier's in the signed ledger:

* ``promoted_by_operator`` — the operator shares a card by hand (for example one
  the classifier kept private). The secret, size and clearance gates still run;
  there is no override for a secret hit.
* ``demoted_by_operator`` — the operator demoted the card's shared copy. The
  shared side holds the operator-signed tombstone; the sweep turns a verified
  tombstone for one of this agent's shared refs into this row.

Both are terminal for the classifier (:func:`~arcmemory.promotion.ledger.needs_judging`),
and a demoted card is never re-promoted — not by the classifier, not by hand.
This module holds the result type and the pure row builders; the sweep owns the
lock, the gates and the publish (one path for both decision makers).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from arcmemory.promotion.ledger import MAX_REASON_CHARS, LedgerRow, PublishState
from arcmemory.promotion.publisher import Demotion
from arcmemory.promotion.render import PromotionText

OperatorShareStatus = Literal[
    "published",
    "outcome_unknown",
    "refused",
    "blocked_secret",
    "too_large",
    "demoted",
    "not_found",
    "clearance_refused",
    "tier_forbidden",
    "disabled",
    "publisher_unavailable",
]


@dataclass(frozen=True)
class OperatorShareResult:
    """What one operator share did. ``shared_ref`` is set only when it was shared."""

    status: OperatorShareStatus
    shared_ref: str | None = None


def demotion_for(row: LedgerRow | None, demotions: Mapping[str, Demotion]) -> Demotion | None:
    """The verified demotion of this card's shared copy, unless already recorded."""
    if row is None or row.decision == "demoted_by_operator" or row.shared_ref is None:
        return None
    return demotions.get(row.shared_ref)


def demoted_row(row: LedgerRow, demotion: Demotion, now: datetime) -> LedgerRow:
    """The sticky ``demoted_by_operator`` row for a card whose shared copy was demoted.

    Keeps the fingerprint the card had when it was shared and the demoted ref.
    """
    return LedgerRow(
        item_kind=row.item_kind,
        item_id=row.item_id,
        content_sha256=row.content_sha256,
        classifier_id=None,
        classifier_version=None,
        question_version=None,
        decision="demoted_by_operator",
        label=None,
        confidence=None,
        personal_probability=None,
        evaluated_at=now,
        publish_state="none",
        shared_ref=demotion.shared_ref,
        decided_by=demotion.decided_by,
        reason=demotion.reason[:MAX_REASON_CHARS] or None,
    )


def operator_promoted_row(
    text: PromotionText,
    *,
    decided_by: str,
    now: datetime,
    publish_state: PublishState,
    shared_ref: str | None,
    declassified: Mapping[str, str] | None = None,
) -> LedgerRow:
    """The ``promoted_by_operator`` row, written once the publish outcome is known.

    ``declassified`` carries the label / clearance / why of a share below the
    agent's clearance (empty otherwise).
    """
    return LedgerRow(
        item_kind=text.item_kind,
        item_id=text.item_id,
        content_sha256=text.content_sha256,
        classifier_id=None,
        classifier_version=None,
        question_version=None,
        decision="promoted_by_operator",
        label=None,
        confidence=None,
        personal_probability=None,
        evaluated_at=now,
        publish_state=publish_state,
        shared_ref=shared_ref,
        decided_by=decided_by,
        **(declassified or {}),
    )


__all__ = [
    "OperatorShareResult",
    "OperatorShareStatus",
    "demoted_row",
    "demotion_for",
    "operator_promoted_row",
]
