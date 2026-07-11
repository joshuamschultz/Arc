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


def test_spool_record_carries_cache_token_breakdown() -> None:
    """Cache read/write tokens ride the llm_call record beside prompt_tokens so
    hit-rate = cache_read / (input + cache_read) is reconstructable downstream."""
    rec = SpoolRecord(
        kind="llm_call",
        actor_did="did:arc:acme:agent:abc123",
        prompt_tokens=1802,
        completion_tokens=45,
        cache_read_tokens=1500,
        cache_write_tokens=300,
    )
    assert rec.cache_read_tokens == 1500
    assert rec.cache_write_tokens == 300


def test_spool_record_cache_tokens_default_none() -> None:
    rec = SpoolRecord(kind="llm_call", actor_did="did:a", prompt_tokens=10)
    assert rec.cache_read_tokens is None
    assert rec.cache_write_tokens is None


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
    kwargs = dict(
        kind="llm_call", actor_did="did:a", request_id="r1", ts="2026-05-31T00:00:00+00:00"
    )
    assert SpoolRecord(**kwargs).record_id == SpoolRecord(**kwargs).record_id
    other = SpoolRecord(
        kind="llm_call", actor_did="did:a", request_id="r2", ts="2026-05-31T00:00:00+00:00"
    )
    assert SpoolRecord(**kwargs).record_id != other.record_id


# SPEC-028 — tool_event + spawn_event kinds (Tasks 1.1, 1.2)


def test_tool_event_fields() -> None:
    """Task 1.1 — a tool_event validates its flat metadata fields + auto ts."""
    rec = SpoolRecord(
        kind="tool_event",
        actor_did="did:arc:acme:agent:abc123",
        request_id="run-1",
        tool_name="web.fetch",
        phase="end",
        outcome="ok",
        latency_ms=42.0,
        args_digest="a" * 64,
        args_size=128,
        result_digest="b" * 64,
        result_size=4096,
    )

    assert rec.kind == "tool_event"
    assert rec.tool_name == "web.fetch"
    assert rec.phase == "end"
    assert rec.args_digest == "a" * 64
    assert rec.args_size == 128
    assert rec.result_digest == "b" * 64
    assert rec.result_size == 4096
    # request_id == run_id so a run's streams join on one key (SDD §11.4).
    assert rec.request_id == "run-1"
    # Metadata-only by default — no body fields populated unless caller opts in.
    assert rec.extra == {}
    assert rec.ts is not None


def test_spawn_event_fields() -> None:
    """Task 1.2 — a spawn_event validates the parent→child lineage edge."""
    rec = SpoolRecord(
        kind="spawn_event",
        actor_did="did:arc:acme:agent:child",
        parent_did="did:arc:acme:agent:parent",
        child_did="did:arc:acme:agent:child",
        role="researcher",
        depth=1,
        outcome="ok",
    )

    assert rec.kind == "spawn_event"
    assert rec.parent_did == "did:arc:acme:agent:parent"
    assert rec.child_did == "did:arc:acme:agent:child"
    assert rec.role == "researcher"
    assert rec.depth == 1
    assert rec.outcome == "ok"


def test_record_id_distinguishes_phase_at_same_ts() -> None:
    """EDGE-3 — tool start+end of one run at the same ts must not collide (no silent drop)."""
    ts = "2026-05-31T00:00:00+00:00"
    common = dict(kind="tool_event", actor_did="did:c", request_id="run-1", ts=ts, tool_name="t")
    start = SpoolRecord(phase="start", **common)
    end = SpoolRecord(phase="end", **common)
    assert start.record_id != end.record_id
