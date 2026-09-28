"""T-1195 (SPEC-083 COMP-016) — the signed, append-only evaluation ledger.

RED intent: ``arcmemory.promotion.ledger`` does not exist yet, so the import fails
with ``ModuleNotFoundError`` — the feature is absent.

Contract under test (SDD COMP-016, README abuse case 6):

- ``PromotionLedger(workspace, signer)`` — append-only JSONL at
  ``<workspace>/memory/promotion/ledger.jsonl`` (direct workspace I/O, ADR-029).
  ``signer`` is the arctrust ``Signer`` protocol (``public_key``, ``algorithm``,
  ``sign``) — the same one ``SharedKnowledgeAdapter`` takes; its public key is the
  pinned key rows must verify against.
- ``LedgerRow`` — ``item_kind, item_id, content_sha256, classifier_id,
  classifier_version, question_version, decision, label, confidence,
  personal_probability, evaluated_at, publish_state, shared_ref, signature``.
  One JSON object per line, keyed by those field names. It never carries content.
- ``append(row) -> LedgerRow`` signs the row over its canonical JSON and returns
  the signed row.
- ``latest(item_kind, item_id) -> LedgerRow | None`` — newest row whose signature
  verifies against the pinned key; tampered / unsigned / foreign-key / garbage
  rows are ignored (never treated as a verdict).
- ``is_current(row, text, classifier, cfg) -> bool`` — True only when
  ``content_sha256``, ``classifier_id``, ``classifier_version`` (vs
  ``cfg.classifier_model``) and ``question_version`` all match.

The signer is the real arctrust ``InProcessSigner`` (Ed25519); no crypto is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from arcmemory.promotion.ledger import LedgerRow, PromotionLedger, is_current
from arctrust.signer import InProcessSigner

_Q1 = "sha256:" + "1" * 64
_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64


@pytest.fixture
def signer() -> InProcessSigner:
    return InProcessSigner(b"\x01" * 32)


@pytest.fixture
def other_signer() -> InProcessSigner:
    return InProcessSigner(b"\x02" * 32)


@pytest.fixture
def ledger(workspace: Path, signer: InProcessSigner) -> PromotionLedger:
    return PromotionLedger(workspace, signer)


@pytest.fixture
def ledger_file(workspace: Path) -> Path:
    return workspace / "memory" / "promotion" / "ledger.jsonl"


def _row(**overrides: object) -> LedgerRow:
    fields: dict[str, object] = {
        "item_kind": "insight",
        "item_id": "acme-renewal",
        "content_sha256": _HASH_A,
        "classifier_id": "jev",
        "classifier_version": "jev-1.13",
        "question_version": _Q1,
        "decision": "keep_private",
        "label": "personal",
        "confidence": 0.97,
        "personal_probability": 0.8,
        "evaluated_at": "2026-09-27T03:00:00+00:00",
        "publish_state": "none",
        "shared_ref": None,
    }
    fields.update(overrides)
    return LedgerRow(**fields)


def _rewrite_line(path: Path, index: int, mutate: object) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[index])
    mutate(record)  # type: ignore[operator]  # test helper: callable mutator
    lines[index] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- location, round trip, signing ---------------------------------------------


def test_append_writes_jsonl_under_workspace_memory_promotion(
    ledger: PromotionLedger, ledger_file: Path
) -> None:
    ledger.append(_row())

    assert ledger_file.is_file()
    assert len(ledger_file.read_text(encoding="utf-8").splitlines()) == 1


def test_append_returns_a_signed_row_that_round_trips(ledger: PromotionLedger) -> None:
    signed = ledger.append(_row())

    assert signed.signature
    assert ledger.latest("insight", "acme-renewal") == signed


def test_round_trip_survives_a_fresh_ledger_instance(
    ledger: PromotionLedger, workspace: Path, signer: InProcessSigner
) -> None:
    """The ledger is durable state, not an in-memory cache (cadence must persist)."""
    signed = ledger.append(_row(decision="promote", label="company", publish_state="pending"))

    reopened = PromotionLedger(workspace, signer)

    assert reopened.latest("insight", "acme-renewal") == signed


def test_blocked_secret_row_without_classifier_fields_round_trips(
    ledger: PromotionLedger,
) -> None:
    signed = ledger.append(
        _row(
            decision="blocked_secret",
            classifier_id=None,
            classifier_version=None,
            question_version=None,
            label=None,
            confidence=None,
            personal_probability=None,
        )
    )

    assert ledger.latest("insight", "acme-renewal") == signed


def test_latest_unknown_item_is_none(ledger: PromotionLedger) -> None:
    assert ledger.latest("insight", "never-seen") is None


def test_latest_is_scoped_by_kind_and_id(ledger: PromotionLedger) -> None:
    ledger.append(_row(item_kind="insight", item_id="acme"))

    assert ledger.latest("procedure", "acme") is None
    assert ledger.latest("insight", "acme-other") is None


# --- append-only ------------------------------------------------------------------


def test_later_row_supersedes_and_earlier_row_remains(
    ledger: PromotionLedger, ledger_file: Path
) -> None:
    ledger.append(_row())
    first_line = ledger_file.read_text(encoding="utf-8").splitlines()[0]

    second = ledger.append(_row(content_sha256=_HASH_B, decision="promote", label="company"))

    lines = ledger_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == first_line
    assert ledger.latest("insight", "acme-renewal") == second


# --- tamper resistance (abuse case 6) ----------------------------------------------


def test_row_with_flipped_decision_is_ignored(ledger: PromotionLedger, ledger_file: Path) -> None:
    """Forcing keep_private -> promote on disk never yields a promote verdict."""
    ledger.append(_row())
    _rewrite_line(ledger_file, 0, lambda r: r.update(decision="promote", label="company"))

    assert ledger.latest("insight", "acme-renewal") is None


def test_row_with_flipped_hash_byte_is_ignored(ledger: PromotionLedger, ledger_file: Path) -> None:
    ledger.append(_row())
    _rewrite_line(ledger_file, 0, lambda r: r.update(content_sha256=_HASH_A[:-1] + "b"))

    assert ledger.latest("insight", "acme-renewal") is None


def test_row_with_missing_signature_is_ignored(ledger: PromotionLedger, ledger_file: Path) -> None:
    ledger.append(_row())
    _rewrite_line(ledger_file, 0, lambda r: r.pop("signature"))

    assert ledger.latest("insight", "acme-renewal") is None


def test_row_with_empty_signature_is_ignored(ledger: PromotionLedger, ledger_file: Path) -> None:
    ledger.append(_row())
    _rewrite_line(ledger_file, 0, lambda r: r.update(signature=""))

    assert ledger.latest("insight", "acme-renewal") is None


def test_row_signed_by_another_key_is_ignored(
    ledger: PromotionLedger, workspace: Path, other_signer: InProcessSigner
) -> None:
    """A row written with a foreign key is not this agent's verdict."""
    PromotionLedger(workspace, other_signer).append(_row(decision="promote", label="company"))

    assert ledger.latest("insight", "acme-renewal") is None


def test_forged_newer_row_does_not_supersede_a_valid_older_row(
    ledger: PromotionLedger, workspace: Path, other_signer: InProcessSigner
) -> None:
    """The newest VERIFYING row wins; a forged newer promote is skipped over."""
    valid = ledger.append(_row())
    PromotionLedger(workspace, other_signer).append(_row(decision="promote", label="company"))

    assert ledger.latest("insight", "acme-renewal") == valid


def test_replayed_signature_on_different_content_is_ignored(
    ledger: PromotionLedger, ledger_file: Path
) -> None:
    """A valid signature lifted from hash H does not authorize a row for hash H'."""
    ledger.append(_row(decision="promote", label="company"))
    original = ledger_file.read_text(encoding="utf-8").splitlines()[0]
    forged = json.loads(original)
    forged["content_sha256"] = _HASH_B
    with ledger_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged) + "\n")

    latest = ledger.latest("insight", "acme-renewal")

    assert latest is not None
    assert latest.content_sha256 == _HASH_A


def test_garbage_lines_are_ignored_not_fatal(ledger: PromotionLedger, ledger_file: Path) -> None:
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    ledger_file.write_text('not json at all\n{"item_kind": 1}\n[]\n', encoding="utf-8")

    assert ledger.latest("insight", "acme-renewal") is None
    signed = ledger.append(_row())
    assert ledger.latest("insight", "acme-renewal") == signed


# --- never carries content -----------------------------------------------------------


def test_row_schema_has_no_content_field() -> None:
    assert {"content", "title", "text", "statement"}.isdisjoint(LedgerRow.model_fields)


def test_row_refuses_a_content_field() -> None:
    """A caller cannot smuggle memory text into the ledger."""
    with pytest.raises(ValueError):
        _row(content="Acme renewal closes at $42k/yr, net-60.")


def test_ledger_file_holds_only_the_documented_keys(
    ledger: PromotionLedger, ledger_file: Path
) -> None:
    ledger.append(_row())

    record = json.loads(ledger_file.read_text(encoding="utf-8").splitlines()[0])

    assert set(record) == {
        "item_kind",
        "item_id",
        "content_sha256",
        "classifier_id",
        "classifier_version",
        "question_version",
        "decision",
        "label",
        "confidence",
        "personal_probability",
        "evaluated_at",
        "publish_state",
        "shared_ref",
        "signature",
    }


# --- is_current ----------------------------------------------------------------------


def _current_args(
    *,
    content_sha256: str = _HASH_A,
    classifier_id: str = "jev",
    question_version: str = _Q1,
    classifier_model: str = "jev-1.13",
) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    return (
        SimpleNamespace(content_sha256=content_sha256),
        SimpleNamespace(classifier_id=classifier_id, question_version=question_version),
        SimpleNamespace(classifier_model=classifier_model),
    )


def test_is_current_true_when_hash_and_versions_all_match(ledger: PromotionLedger) -> None:
    row = ledger.append(_row())

    assert is_current(row, *_current_args()) is True


@pytest.mark.parametrize(
    "change",
    [
        {"content_sha256": _HASH_B},
        {"classifier_model": "jev-1.14"},
        {"question_version": "sha256:" + "2" * 64},
        {"classifier_id": "other"},
    ],
    ids=[
        "content_changed",
        "classifier_version_changed",
        "question_changed",
        "classifier_changed",
    ],
)
def test_is_current_false_when_anything_differs(
    ledger: PromotionLedger, change: dict[str, str]
) -> None:
    """A stale verdict is never reused — the item is re-evaluated (REQ-499)."""
    row = ledger.append(_row())

    assert is_current(row, *_current_args(**change)) is False
