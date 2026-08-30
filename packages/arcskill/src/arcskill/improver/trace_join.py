"""Read-time trace join — improver span metadata joined to arcllm payloads (H-041).

The improver's :class:`~arcskill.improver.trace_store.TraceStore` owns skill-usage
span *metadata* (which skill, which tools, outcome, and the arcllm ``llm_trace_ids``
each span touched). The full LLM request/response *payloads* live in arcllm's
``JSONLTraceStore``. Curation needs to show an operator the real body of a used
trace — so this module JOINs the two by trace id **at read time**. No payload is ever
copied into a second store (locked design §3, LLM02/LLM07).

The arcllm store is reached through a tiny structural seam (:class:`PayloadSource`) so
arcskill takes no hard arcllm dependency — arcagent injects an adapter over the real
``JSONLTraceStore.get``; tests inject a dict-backed fake.

Federal hash-only trace mode: when a span's payloads are all sealed/absent (encrypted
envelope or capture disabled), curation is *declared unavailable* with the threat-model
reason — a typed :class:`CurationUnavailable`, never a silently-empty list (locked
design §3; a data connection must not vanish without a declared reason).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from arcskill.improver.models import SkillTrace

# Resolve one arcllm trace payload by id → the record dict, or None if absent.
PayloadSource = Callable[[str], "Awaitable[dict[str, Any] | None]"]


@runtime_checkable
class SpanSource(Protocol):
    """Structural source of skill spans (the improver TraceStore satisfies it)."""

    def load_traces(self, skill_name: str) -> list[SkillTrace]: ...


@dataclass(frozen=True)
class JoinedTrace:
    """One skill span with its arcllm payloads resolved at read time."""

    span: SkillTrace
    payloads: list[dict[str, Any]] = field(default_factory=list)

    @property
    def trace_id(self) -> str:
        return self.span.trace_id

    def body_text(self) -> str:
        """A human-readable rendering of the joined request/response bodies.

        This is the raw material an operator edits toward the ideal, and the exact
        text the emission path redacts BEFORE it is written as a golden (LLM02).
        """
        chunks: list[str] = []
        for record in self.payloads:
            req = record.get("request_body")
            resp = record.get("response_body")
            if req is not None:
                chunks.append(f"REQUEST({record.get('trace_id', '')}):\n{req}")
            if resp is not None:
                chunks.append(f"RESPONSE({record.get('trace_id', '')}):\n{resp}")
        return "\n\n".join(chunks)


@dataclass(frozen=True)
class CurationUnavailable:
    """Curation is declared unavailable for a span, with a threat-model reason.

    Returned instead of an empty join so a hash-only (federal) or capture-disabled
    deployment never looks like "no traces" — the reason is explicit and auditable.
    """

    trace_id: str
    reason: str


def _payload_is_readable(record: dict[str, Any] | None) -> bool:
    """A payload is curatable only if a plaintext body is actually present.

    A sealed envelope (``encryption`` set, bodies ``None``) or capture-disabled record
    carries no readable body — federal hash-only mode lands here.
    """
    if record is None:
        return False
    if record.get("encryption") is not None:
        return False
    return record.get("request_body") is not None or record.get("response_body") is not None


class TraceJoin:
    """Joins improver spans to arcllm payloads by trace id, read-time only."""

    def __init__(self, spans: SpanSource, payloads: PayloadSource) -> None:
        self._spans = spans
        self._payloads = payloads

    async def join_one(self, span: SkillTrace) -> JoinedTrace | CurationUnavailable:
        """Resolve one span's payloads; declare unavailable if none are readable."""
        if not span.llm_trace_ids:
            return CurationUnavailable(
                trace_id=span.trace_id,
                reason="no linked LLM trace ids (heuristic-only span; nothing to curate)",
            )
        resolved: list[dict[str, Any]] = []
        for tid in span.llm_trace_ids:
            record = await self._payloads(tid)
            if record is None or not _payload_is_readable(record):
                continue
            resolved.append(record)
        if not resolved:
            return CurationUnavailable(
                trace_id=span.trace_id,
                reason=(
                    "trace payloads are hash-only or sealed at rest "
                    "(federal hash-only trace mode / capture disabled): curation unavailable"
                ),
            )
        return JoinedTrace(span=span, payloads=resolved)

    async def curatable(
        self, skill_name: str
    ) -> list[JoinedTrace | CurationUnavailable]:
        """Every span for ``skill_name`` joined to its payloads (read-time), newest ids first.

        Each element is either a :class:`JoinedTrace` (payloads visible) or a
        :class:`CurationUnavailable` (declared, with reason) — never a silent drop.
        """
        return [await self.join_one(span) for span in self._spans.load_traces(skill_name)]

    async def resolve(self, skill_name: str, trace_id: str) -> JoinedTrace | CurationUnavailable:
        """Join a single span by its improver ``trace_id`` (the CLI/arcui entry point)."""
        for span in self._spans.load_traces(skill_name):
            if span.trace_id == trace_id:
                return await self.join_one(span)
        return CurationUnavailable(trace_id=trace_id, reason="no such trace for this skill")


__all__ = [
    "CurationUnavailable",
    "JoinedTrace",
    "PayloadSource",
    "SpanSource",
    "TraceJoin",
]
