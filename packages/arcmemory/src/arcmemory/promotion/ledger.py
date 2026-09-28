"""Signed, append-only promotion evaluation ledger (SPEC-083 COMP-016).

One JSON row per evaluation at ``<workspace>/memory/promotion/ledger.jsonl``.
Agent state stays in the agent's workspace, written with direct I/O (ADR-029).
The ledger lets an item leave the box once, not every night: a verdict is reused
only while its content hash, classifier id + version and question version all
still match (:func:`is_current`).

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

LedgerDecision = Literal["promote", "keep_private", "blocked_secret", "too_large"]
PublishState = Literal["none", "pending", "published", "outcome_unknown"]

#: Decisions reached without calling the classifier; their rows carry no verdict.
_PRE_EGRESS_DECISIONS = frozenset({"blocked_secret", "too_large"})


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
        if self.decision in _PRE_EGRESS_DECISIONS:
            if any(field is not None for field in (*verdict, self.personal_probability)):
                raise ValueError(f"a {self.decision} row carries no classifier verdict")
        elif any(field is None for field in verdict):
            raise ValueError(f"a {self.decision} row requires the full classifier verdict")
        return self

    def signing_payload(self) -> bytes:
        """The canonical bytes the signature binds (every field but ``signature``)."""
        return canonical_json(self.model_dump(mode="json", exclude={"signature"}))


class _HashedText(Protocol):
    @property
    def content_sha256(self) -> str: ...


class _VersionedClassifier(Protocol):
    @property
    def classifier_id(self) -> str: ...

    @property
    def question_version(self) -> str: ...


class _PinnedModel(Protocol):
    @property
    def classifier_model(self) -> str: ...


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
        line = json.dumps(signed.model_dump(mode="json"), sort_keys=True) + "\n"
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


def is_current(
    row: LedgerRow, text: _HashedText, classifier: _VersionedClassifier, cfg: _PinnedModel
) -> bool:
    """True only when the row judged these exact bytes with this classifier and question."""
    return (
        row.content_sha256 == text.content_sha256
        and row.classifier_id == classifier.classifier_id
        and row.classifier_version == cfg.classifier_model
        and row.question_version == classifier.question_version
    )


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


__all__ = ["LedgerDecision", "LedgerRow", "PromotionLedger", "PublishState", "is_current"]
