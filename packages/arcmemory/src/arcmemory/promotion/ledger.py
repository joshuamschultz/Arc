"""Signed, append-only promotion evaluation ledger (SPEC-083 COMP-016).

One JSON row per evaluation at ``<workspace>/memory/promotion/ledger.jsonl``.
Agent state stays in the agent's workspace, written with direct I/O (ADR-029).
The ledger lets an item leave the box once, not every night: every card holds one
durable decision. A classifier verdict holds until the card's bytes change (its
content fingerprint); a model or question bump is not new facts, so it re-judges
nothing. An operator decision (``promoted_by_operator`` / ``demoted_by_operator``,
alpha-2 item 16) holds whatever the bytes become: the classifier never overrides
an operator, and a demoted card never re-promotes (:func:`needs_judging`).

Every row is signed over its canonical JSON by the agent's injected signer; a row
that does not verify against that signer's public key is ignored — never read as
a verdict (abuse case 6). A row never carries memory content.

Scale: :meth:`PromotionLedger.latest_index` reads the file once, newest-first,
and verifies at most one signature per item that resolves (plus any forged rows
it skips), so a nightly sweep is linear in the ledger size — never items x rows.
The file still grows by at most ``max_items_per_sweep`` evaluation rows a night
(plus publish-state rows); compaction is the path if a single read ever bites.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Protocol

from arctrust import canonical_json
from arctrust.signer import Signer, verify_signature
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError, model_validator

from arcmemory.promotion.classifier import PromotionLabel
from arcmemory.promotion.render import PromotableKind

LedgerDecision = Literal[
    "promote",
    "keep_private",
    "blocked_secret",
    "too_large",
    "promoted_by_operator",
    "demoted_by_operator",
]
PublishState = Literal["none", "pending", "published", "outcome_unknown"]

#: Decisions reached without calling the classifier; their rows carry no verdict.
_PRE_EGRESS_DECISIONS = frozenset({"blocked_secret", "too_large"})
#: Decisions an operator made; they name ``decided_by`` and are never re-judged.
OPERATOR_DECISIONS = frozenset({"promoted_by_operator", "demoted_by_operator"})
#: Rows that carry no classifier verdict at all.
_NO_VERDICT_DECISIONS = _PRE_EGRESS_DECISIONS | OPERATOR_DECISIONS
#: Optional operator fields. Omitted from the stored (and signed) bytes when
#: absent, so a row signed before they existed still verifies.
_OPERATOR_FIELDS = frozenset({"decided_by", "reason"})
#: Optional declassified-at-source fields: the label a card was shared under, the
#: clearance of the sharing agent, and why a lower label was allowed. Omitted from
#: the stored (and signed) bytes when absent, like the operator fields.
_DECLASS_FIELDS = frozenset({"shared_label", "share_clearance", "declassified_why"})
DECLASSIFIED_WHY = (
    "card's stored label is below the agent's clearance; shared at its own label "
    "after the secret gate and the sharing decision"
)
MAX_REASON_CHARS = 500
_MAX_DID_CHARS = 256


class LedgerRow(BaseModel):
    """One evaluation of one item. No content; ``signature`` is hex over the rest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_kind: PromotableKind
    item_id: str
    content_sha256: str
    classifier_id: str | None
    classifier_version: str | None
    question_version: str | None
    decision: LedgerDecision
    label: PromotionLabel | None
    confidence: float | None
    personal_probability: float | None
    evaluated_at: AwareDatetime
    publish_state: PublishState
    shared_ref: str | None
    decided_by: str | None = None
    reason: str | None = None
    shared_label: str | None = None
    share_clearance: str | None = None
    declassified_why: str | None = None
    signature: str = ""

    @model_validator(mode="after")
    def _verdict_fields_match_decision(self) -> LedgerRow:
        verdict = (
            self.classifier_id,
            self.classifier_version,
            self.question_version,
            self.label,
            self.confidence,
        )
        if self.decision in _NO_VERDICT_DECISIONS:
            if any(field is not None for field in (*verdict, self.personal_probability)):
                raise ValueError(f"a {self.decision} row carries no classifier verdict")
        elif any(field is None for field in verdict):
            raise ValueError(f"a {self.decision} row requires the full classifier verdict")
        self._check_operator_fields()
        declass = (self.shared_label, self.share_clearance, self.declassified_why)
        if any(value is not None for value in declass) and any(value is None for value in declass):
            raise ValueError("a declassified share records its label, clearance and why together")
        return self

    def _check_operator_fields(self) -> None:
        """Only an operator row names a decider; it must be a DID, the reason bounded."""
        if self.decision not in OPERATOR_DECISIONS:
            if self.decided_by is not None or self.reason is not None:
                raise ValueError(f"a {self.decision} row names no operator decider")
            return
        decider = self.decided_by
        if not decider or not decider.startswith("did:") or len(decider) > _MAX_DID_CHARS:
            raise ValueError(f"a {self.decision} row requires the deciding operator's DID")
        if self.reason is not None and len(self.reason) > MAX_REASON_CHARS:
            raise ValueError(f"an operator reason is at most {MAX_REASON_CHARS} characters")

    def stored(self) -> dict[str, object]:
        """The JSON form on disk: an absent operator field is omitted, not ``null``."""
        optional = _OPERATOR_FIELDS | _DECLASS_FIELDS
        absent = {name for name in optional if getattr(self, name) is None}
        return self.model_dump(mode="json", exclude=absent)

    def signing_payload(self) -> bytes:
        """The canonical bytes the signature binds (every stored field but ``signature``)."""
        payload = self.stored()
        payload.pop("signature", None)
        return canonical_json(payload)


class _HashedText(Protocol):
    @property
    def content_sha256(self) -> str: ...


class PromotionLedger:
    """Append and look up signed evaluation rows for one agent."""

    def __init__(self, workspace: Path, signer: Signer) -> None:
        self._path = workspace / "memory" / "promotion" / "ledger.jsonl"
        self._signer = signer

    def append(self, row: LedgerRow) -> LedgerRow:
        """Sign ``row``, append it durably (fsync), and return the signed row."""
        signed = row.model_copy(
            update={"signature": self._signer.sign(row.signing_payload()).hex()}
        )
        line = json.dumps(signed.stored(), sort_keys=True) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A crash mid-append can leave a torn last line; start on a fresh line so
        # the new row is never glued onto the garbage (which is then ignored).
        prefix = "\n" if _ends_mid_line(self._path) else ""
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + line)
            handle.flush()
            os.fsync(handle.fileno())
        return signed

    def latest_index(self) -> dict[tuple[str, str], LedgerRow]:
        """The newest verified row for every ``(item_kind, item_id)``, in one pass.

        Walks the file newest-first; once an item has a verified row, its older rows
        are skipped without a signature check. A row that fails verification is
        ignored, so an older verified row for the same item can still resolve.
        """
        index: dict[tuple[str, str], LedgerRow] = {}
        if not self._path.is_file():
            return index
        lines = self._path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            row = _parse_row(line)
            if row is None:
                continue
            key = (row.item_kind, row.item_id)
            if key not in index and self._verifies(row):
                index[key] = row
        return index

    def latest(self, item_kind: str, item_id: str) -> LedgerRow | None:
        """The newest verified row for one item; else ``None``. One full read per call."""
        return self.latest_index().get((item_kind, item_id))

    def history(self, item_kind: str, item_id: str) -> list[LedgerRow]:
        """Every verified row for one item, oldest first — its decision history.

        Forged, unsigned and garbage rows are left out, exactly as
        :meth:`latest_index` ignores them. One full read per call.
        """
        if not self._path.is_file():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        rows = (_parse_row(line) for line in lines)
        return [
            row
            for row in rows
            if row is not None
            and (row.item_kind, row.item_id) == (item_kind, item_id)
            and self._verifies(row)
        ]

    def _verifies(self, row: LedgerRow) -> bool:
        try:
            signature = bytes.fromhex(row.signature)
        except ValueError:
            return False
        if not signature:
            return False
        return verify_signature(
            self._signer.algorithm, row.signing_payload(), signature, self._signer.public_key
        )


def needs_judging(row: LedgerRow, text: _HashedText) -> bool:
    """True when the card behind ``row`` must be decided again.

    An operator decision never is: the classifier never overrides an operator,
    and a demoted card never re-promotes, whatever its bytes become. Any other
    decision holds until the card's content fingerprint changes.
    """
    if row.decision in OPERATOR_DECISIONS:
        return False
    return row.content_sha256 != text.content_sha256


def _parse_row(line: str) -> LedgerRow | None:
    """Parse one JSONL line; garbage or schema-invalid lines are skipped, not fatal."""
    try:
        return LedgerRow.model_validate(json.loads(line))
    except (json.JSONDecodeError, ValidationError):
        return None


def _ends_mid_line(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) != b"\n"


__all__ = [
    "DECLASSIFIED_WHY",
    "MAX_REASON_CHARS",
    "OPERATOR_DECISIONS",
    "LedgerDecision",
    "LedgerRow",
    "PromotionLedger",
    "PublishState",
    "needs_judging",
]
