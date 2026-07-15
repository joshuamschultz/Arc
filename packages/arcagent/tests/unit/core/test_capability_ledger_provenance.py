"""SPEC-035 approval enrichment — SessionCapabilityLedger leg provenance.

The ledger accumulates trifecta legs AND records, per session, which tool call
lit each leg (with a short redacted argument summary and a timestamp) so a later
trifecta block can be explained leg-by-leg to the operator.
"""

from __future__ import annotations

from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    PRIVATE_DATA,
    ProvenanceEntry,
    SessionCapabilityLedger,
)


def test_record_appends_provenance_entry() -> None:
    ledger = SessionCapabilityLedger()
    ledger.record(
        "s1", frozenset({PRIVATE_DATA}), tool_name="file_read", arg_summary="path=/etc/hosts"
    )

    prov = ledger.provenance("s1")
    assert len(prov) == 1
    entry = prov[0]
    assert isinstance(entry, ProvenanceEntry)
    assert entry.legs == (PRIVATE_DATA,)
    assert entry.tool_name == "file_read"
    assert entry.arg_summary == "path=/etc/hosts"
    assert entry.at  # a timestamp was stamped


def test_provenance_is_ordered_by_record_call() -> None:
    ledger = SessionCapabilityLedger()
    ledger.record("s1", frozenset({PRIVATE_DATA}), tool_name="file_read", arg_summary="a")
    ledger.record("s1", frozenset({EXTERNAL_COMMS}), tool_name="messaging_send", arg_summary="b")

    tools = [e.tool_name for e in ledger.provenance("s1")]
    assert tools == ["file_read", "messaging_send"]


def test_empty_legs_record_no_provenance() -> None:
    ledger = SessionCapabilityLedger()
    ledger.record("s1", frozenset(), tool_name="noop", arg_summary="x")
    assert ledger.provenance("s1") == []


def test_provenance_is_per_session() -> None:
    ledger = SessionCapabilityLedger()
    ledger.record("s1", frozenset({PRIVATE_DATA}), tool_name="file_read")
    assert ledger.provenance("s2") == []


def test_reset_clears_provenance() -> None:
    ledger = SessionCapabilityLedger()
    ledger.record("s1", frozenset({PRIVATE_DATA}), tool_name="file_read")
    ledger.reset("s1")
    assert ledger.provenance("s1") == []


def test_multiple_legs_are_sorted_in_entry() -> None:
    ledger = SessionCapabilityLedger()
    ledger.record("s1", frozenset({EXTERNAL_COMMS, PRIVATE_DATA}), tool_name="t")
    assert ledger.provenance("s1")[0].legs == tuple(sorted((EXTERNAL_COMMS, PRIVATE_DATA)))


def test_as_dict_is_json_ready() -> None:
    entry = ProvenanceEntry(
        legs=(PRIVATE_DATA, EXTERNAL_COMMS), tool_name="t", arg_summary="s", at="2026-01-01T00:00:00+00:00"
    )
    assert entry.as_dict() == {
        "legs": [PRIVATE_DATA, EXTERNAL_COMMS],
        "tool": "t",
        "args": "s",
        "at": "2026-01-01T00:00:00+00:00",
    }
