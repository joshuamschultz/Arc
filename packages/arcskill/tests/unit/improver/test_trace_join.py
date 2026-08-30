"""Read-time trace join: payloads visible, federal hash-only declared (H-041)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from arcskill.improver.models import SkillTrace
from arcskill.improver.trace_join import CurationUnavailable, JoinedTrace, TraceJoin


class _Spans:
    def __init__(self, traces: list[SkillTrace]) -> None:
        self._traces = traces

    def load_traces(self, skill_name: str) -> list[SkillTrace]:
        return [t for t in self._traces if t.skill_name == skill_name]


def _span(trace_id: str, llm_ids: list[str]) -> SkillTrace:
    return SkillTrace(
        trace_id=trace_id,
        session_id="s",
        skill_name="sk",
        skill_version=1,
        turn_number=0,
        started_at=datetime.now(UTC),
        llm_trace_ids=llm_ids,
    )


def _payload_source(records: dict[str, dict[str, Any]]):
    async def resolve(tid: str) -> dict[str, Any] | None:
        return records.get(tid)

    return resolve


async def test_join_resolves_payloads_at_read_time() -> None:
    records = {
        "llm-1": {"trace_id": "llm-1", "request_body": {"q": "hi"}, "response_body": {"a": "yo"}},
    }
    join = TraceJoin(_Spans([_span("t1", ["llm-1"])]), _payload_source(records))
    result = await join.resolve("sk", "t1")
    assert isinstance(result, JoinedTrace)
    assert result.payloads[0]["request_body"] == {"q": "hi"}
    assert "hi" in result.body_text() and "yo" in result.body_text()


async def test_federal_hash_only_is_declared_unavailable_not_empty() -> None:
    # Sealed envelope (bodies None) = federal hash-only trace mode.
    records = {
        "llm-1": {"trace_id": "llm-1", "request_body": None, "response_body": None,
                  "encryption": {"alg": "AES-256-GCM"}},
    }
    join = TraceJoin(_Spans([_span("t1", ["llm-1"])]), _payload_source(records))
    result = await join.resolve("sk", "t1")
    assert isinstance(result, CurationUnavailable)
    assert "hash-only" in result.reason


async def test_span_with_no_linked_llm_ids_is_declared_unavailable() -> None:
    join = TraceJoin(_Spans([_span("t1", [])]), _payload_source({}))
    result = await join.resolve("sk", "t1")
    assert isinstance(result, CurationUnavailable)
    assert "nothing to curate" in result.reason


async def test_curatable_lists_each_span_with_a_typed_outcome() -> None:
    records = {"llm-1": {"trace_id": "llm-1", "response_body": {"a": 1}}}
    spans = _Spans([_span("t1", ["llm-1"]), _span("t2", [])])
    results = await TraceJoin(spans, _payload_source(records)).curatable("sk")
    assert isinstance(results[0], JoinedTrace)
    assert isinstance(results[1], CurationUnavailable)
