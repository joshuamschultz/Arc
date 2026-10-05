"""Context prep — the turn's one pre-model retrieval, bounded and fully traced.

Before this stage existed a chat turn ran three separate retrieval passes as
bus hooks (the user-turn proactive moments, assembly-time recall and a
pre-respond insight recall), each bounded only by the 30 s per-handler bus
timeout. On a loaded host they stacked past the 120 s turn-start guard and the
chat failed before its first model call, with nothing in the trace to say why.

Now there is exactly one pass, owned here:

* **Bounded.** The whole retrieval runs under ``[session]
  context_prep_budget_seconds``. On timeout the turn continues WITHOUT retrieved
  context; a WARNING names the step and the run trace gets ``context.skipped``.
* **Small.** Memory and connected documents each have a top-k and a token cap;
  documents also a score floor; the sum is capped by ``context_token_cap``.
* **Visible.** One ``context.retrieval`` run event carries the query, each
  sub-step's latency, every candidate (source, title or path, score,
  classification, a short redacted snippet, used or excluded and why) and the
  tokens injected.

The retrieval itself is served by whichever capability registers the
``context_retrieval`` operation contract (the memory module); core names no
module (ADR-033). It returns candidates; ranking, caps, rendering and the trace
are decided here, so every Brain behaves the same way at this boundary.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arcagent.extension.untrusted import frame_untrusted
from arcagent.tools._secret_guard import redact_tool_event_value

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import SessionConfig

_logger = logging.getLogger("arcagent.context_prep")

#: The operation contract the retrieval capability registers under (ADR-033).
CONTEXT_RETRIEVAL = "context_retrieval"
#: Characters of a candidate shown in the trace — a glimpse, never the body.
_SNIPPET_CHARS = 160
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token count (4 characters a token), the estimate used for every cap here."""
    return math.ceil(len(text) / _CHARS_PER_TOKEN) if text else 0


@dataclass(frozen=True)
class Candidate:
    """One retrieved item before selection."""

    source_kind: str
    source: str
    title: str
    path: str
    score: float
    classification: str
    text: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass
class ContextPrep:
    """What the stage produced: the text to inject and the trace record behind it."""

    text: str = ""
    trace: dict[str, Any] = field(default_factory=dict)
    #: Steps that did not finish (``{"step", "reason"}``), each a ``context.skipped`` event.
    skipped: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Caps:
    memory_top_k: int
    memory_tokens: int
    docs_top_k: int
    docs_tokens: int
    docs_floor: float
    memory_floor: float
    total_tokens: int

    @classmethod
    def of(cls, config: SessionConfig) -> _Caps:
        return cls(
            memory_top_k=config.context_memory_top_k,
            memory_tokens=config.context_memory_token_cap,
            docs_top_k=config.context_docs_top_k,
            docs_tokens=config.context_docs_token_cap,
            docs_floor=config.context_docs_score_floor,
            memory_floor=config.context_memory_score_floor,
            total_tokens=config.context_token_cap,
        )


async def prepare_context(agent: ArcAgent, query: str) -> ContextPrep:
    """Run the bounded retrieval for ``query``; the caller records the trace in run order.

    Never raises and never outlives the budget: a slow, failing or absent
    retrieval yields an empty :class:`ContextPrep` and a trace that says which.
    """
    config = agent._config.session
    caps = _Caps.of(config)
    budget = config.context_prep_budget_seconds
    started = time.monotonic()
    trace: dict[str, Any] = {
        "status": "ok",
        "query": _snippet(query, limit=400),
        "budget_ms": round(budget * 1000.0, 1),
        "top_k": caps.memory_top_k + caps.docs_top_k,
        "token_cap": caps.total_tokens,
        "tokens_injected": 0,
        "steps": [],
        "items": [],
    }
    retrieve = await _retrieval_operation(agent)
    if retrieve is None or not query.strip():
        trace["status"] = "skipped"
        trace["reason"] = "no retrieval capability" if retrieve is None else "empty request"
        return _finish(started, ContextPrep(trace=trace))
    try:
        async with asyncio.timeout(budget):
            found = await retrieve(
                query, memory_top_k=caps.memory_top_k, docs_top_k=caps.docs_top_k
            )
    except TimeoutError:
        _logger.warning(
            "context prep: retrieval timed out after %.1fs; the turn continues without it",
            budget,
        )
        trace["status"] = "timeout"
        trace["reason"] = f"retrieval timed out after {budget:g}s"
        skipped = [{"step": "retrieval", "reason": "timed out"}]
        return _finish(started, ContextPrep(trace=trace, skipped=skipped))
    except Exception as exc:  # reason: retrieval is best-effort; the turn must go on
        _logger.warning(
            "context prep: retrieval failed; the turn continues without it", exc_info=True
        )
        trace["status"] = "error"
        trace["reason"] = type(exc).__name__
        return _finish(started, ContextPrep(trace=trace))
    candidates = [_as_candidate(raw) for raw in _seq(found.get("candidates"))]
    trace["steps"] = [dict(step) for step in _seq(found.get("steps"))]
    chosen, items = select_candidates(candidates, caps)
    text = frame_untrusted([(c.path or c.source, c.text) for c in chosen]) if chosen else ""
    trace["items"] = items
    trace["tokens_injected"] = estimate_tokens(text)
    return _finish(started, ContextPrep(text=text, trace=trace))


def select_candidates(
    candidates: Sequence[Candidate], caps: _Caps
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Choose what is injected; explain every candidate, kept or not.

    Duplicates (same source, or same text) collapse onto the first seen. Each
    source kind is taken best score first, within its own top-k and token cap;
    a candidate with no relevance (score 0) or below its lane's floor is
    dropped; the running total never passes ``total_tokens``. Profile facts
    and staged loop recalls are memory.
    """
    chosen: list[Candidate] = []
    items: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str]] = set()
    seen_text: set[str] = set()
    used = {"memory": [0, 0], "connection": [0, 0]}  # [count, tokens] per lane
    total = 0
    for cand in sorted(candidates, key=lambda c: (_lane(c) != "memory", -c.score)):
        lane = _lane(cand)
        top_k, cap = (
            (caps.memory_top_k, caps.memory_tokens)
            if lane == "memory"
            else (caps.docs_top_k, caps.docs_tokens)
        )
        key = (cand.source_kind, cand.source + "#" + cand.path)
        reason = ""
        if not cand.text.strip():
            reason = "empty"
        elif key in seen_sources or cand.text in seen_text:
            reason = "duplicate"
        elif cand.score <= 0.0:
            reason = "no relevance (score 0)"
        elif cand.score < (
            floor := caps.docs_floor if lane == "connection" else caps.memory_floor
        ):
            reason = f"score below floor {floor:g}"
        elif used[lane][0] >= top_k:
            reason = f"below top-{top_k}"
        elif used[lane][1] + cand.tokens > cap:
            reason = f"over the {lane} token cap ({cap})"
        elif total + cand.tokens > caps.total_tokens:
            reason = f"over the total token cap ({caps.total_tokens})"
        seen_sources.add(key)
        seen_text.add(cand.text)
        if not reason:
            chosen.append(cand)
            used[lane][0] += 1
            used[lane][1] += cand.tokens
            total += cand.tokens
        items.append(_trace_item(cand, included=not reason, reason=reason or "used"))
    return chosen, items


def _lane(cand: Candidate) -> str:
    return "connection" if cand.source_kind == "connection" else "memory"


def _trace_item(cand: Candidate, *, included: bool, reason: str) -> dict[str, Any]:
    """A candidate as the trace shows it: provenance and a short, safe glimpse."""
    snippet = (
        _snippet(cand.text)
        if cand.classification in ("", "unclassified")
        else f"[{cand.classification}: withheld from the trace]"
    )
    return {
        "source_kind": cand.source_kind,
        "source": cand.source,
        "title": cand.title,
        "path": cand.path,
        "score": round(cand.score, 4),
        "classification": cand.classification,
        "snippet": snippet,
        "tokens": cand.tokens,
        "included": included,
        "reason": reason,
    }


def _snippet(text: str, *, limit: int = _SNIPPET_CHARS) -> str:
    """Whitespace-collapsed, secret-redacted, truncated text for the trace."""
    flat = " ".join(str(redact_tool_event_value(text)).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _as_candidate(raw: Any) -> Candidate:
    data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return Candidate(
        source_kind=str(data.get("source_kind", "memory")),
        source=str(data.get("source", "")),
        title=str(data.get("title", "")),
        path=str(data.get("path", "")),
        score=float(data.get("score", 0.0) or 0.0),
        classification=str(data.get("classification", "unclassified")),
        text=str(data.get("text", "")),
    )


def _seq(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list | tuple) else ()


async def _retrieval_operation(agent: ArcAgent) -> Any:
    """The active ``context_retrieval`` capability's ``retrieve``, or ``None``."""
    registry = agent._capability_registry
    if registry is None:
        return None
    entry = await registry.get_capability(CONTEXT_RETRIEVAL)
    if entry is None or not entry.setup_done:
        return None
    return getattr(entry.instance, "retrieve", None)


def _finish(started: float, prep: ContextPrep) -> ContextPrep:
    prep.trace["latency_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    return prep


def spool_record(rec: Any) -> None:
    """Append one record to the operational spool (ArcStore is optional in core)."""
    from arcstore.spool import record

    record(rec)


def spool_run_event(
    agent: ArcAgent, run_id: str, name: str, extra: dict[str, Any], *, outcome: str = "ok"
) -> None:
    """One pre-model step as a ``run_event`` on this run's timeline (fail-open).

    Without ArcStore installed there is no spool, so nothing is recorded.
    """
    try:
        from arcstore.records import SpoolRecord
    except ImportError:
        return
    actor = agent._identity.did if agent._identity is not None else "did:arc:unknown"
    spool_record(
        SpoolRecord(
            kind="run_event",
            actor_did=actor,
            request_id=run_id,
            name=name,
            outcome=outcome,
            extra=extra,
        )
    )


__all__ = [
    "CONTEXT_RETRIEVAL",
    "Candidate",
    "ContextPrep",
    "estimate_tokens",
    "prepare_context",
    "select_candidates",
    "spool_run_event",
]
