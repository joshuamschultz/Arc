"""Unit tests for arcstore.records.SpoolRecord (SPEC-026 FR-2, Task 1.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcstore.records import SpoolRecord


def test_spool_record_llm_call_fields_and_auto_ts() -> None:
    rec = SpoolRecord(
        kind="llm_call",
        actor_did="did:arc:acme:agent:abc123",
        request_id="req-1",
        model="anthropic/claude-opus-4-8",
        prompt_tokens=120,
        completion_tokens=45,
        cost_usd=0.0123,
        latency_ms=812.5,
        outcome="ok",
    )

    assert rec.kind == "llm_call"
    assert rec.actor_did == "did:arc:acme:agent:abc123"
    assert rec.model == "anthropic/claude-opus-4-8"
    assert rec.prompt_tokens == 120
    assert rec.completion_tokens == 45
    assert rec.cost_usd == 0.0123
    assert rec.latency_ms == 812.5
    assert rec.outcome == "ok"
    # ts auto-populated as ISO-8601 UTC when omitted
    assert rec.ts is not None
    assert rec.ts.endswith("+00:00") or rec.ts.endswith("Z")


def test_spool_record_is_frozen() -> None:
    rec = SpoolRecord(kind="run_event", actor_did="did:arc:x:agent:y", name="step")
    with pytest.raises(ValidationError):
        rec.outcome = "error"  # type: ignore[misc]


def test_extra_default_is_independent_per_instance() -> None:
    a = SpoolRecord(kind="agent_event", actor_did="did:a")
    b = SpoolRecord(kind="agent_event", actor_did="did:b")
    assert a.extra == {} and b.extra == {}
    assert a.extra is not b.extra  # no shared mutable default


def test_explicit_ts_is_preserved() -> None:
    rec = SpoolRecord(kind="llm_call", actor_did="did:a", ts="2026-05-31T00:00:00+00:00")
    assert rec.ts == "2026-05-31T00:00:00+00:00"


def test_record_id_is_stable_and_content_derived() -> None:
    kwargs = dict(kind="llm_call", actor_did="did:a", request_id="r1", ts="2026-05-31T00:00:00+00:00")
    assert SpoolRecord(**kwargs).record_id == SpoolRecord(**kwargs).record_id
    other = SpoolRecord(kind="llm_call", actor_did="did:a", request_id="r2", ts="2026-05-31T00:00:00+00:00")
    assert SpoolRecord(**kwargs).record_id != other.record_id
