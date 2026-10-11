"""Work-event stream, page and published key (SPEC-088 COMP-014).

``GET /api/work-events/stream`` — Server-Sent Events: ``id: <seq>``,
                                  ``event: <kind>``, ``data: <envelope JSON>``;
                                  ``Last-Event-ID`` resumes after that ``seq``;
                                  a comment frame at least every 15 s.
``GET /api/work-events``        — the same events as a JSON page
                                  (``after``, ``limit``) for clients that cannot
                                  hold a stream.
``GET /api/work-events/keys``   — the work-events verify key
                                  ``{key_id, algorithm, public_key}``; 503 when
                                  no work-events key exists. Never the operator key.

All three take ``agent`` and call ``require_scope(events:subscribe,
agent_did=agent)``: an app must name an agent its scope is bound to; operator
and viewer sessions may omit it and see every agent. Filters: ``run_id``,
``correlation_id`` and ``kinds`` (comma-separated). Only events whose
classification the deployment clearance dominates are sent, and only pointer
envelopes (D-849). At most four open streams per principal DID; opening and
closing a stream are each one ``ui.mutation`` audit row.

Optional (REQ-633): when this module is physically absent the routes are not
served and the rest of arcui is unaffected.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from arcstore.work_events import MAX_PAGE, WorkEvent, WorkEventLog
from arctrust.classification import Classification, dominates, parse_classification
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from arcui.audit import emit_mutation_audit
from arcui.principals import Scope as PrincipalScope
from arcui.principals import require_scope
from arcui.work_event_envelope import pointer_envelope

__all__ = ["HEARTBEAT_SECONDS", "MAX_STREAMS_PER_PRINCIPAL", "MAX_STREAM_SECONDS", "routes"]

MAX_STREAMS_PER_PRINCIPAL = 4
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 1.0
#: Scope is checked when a stream opens. Ending each stream after an hour makes a
#: revoked or de-scoped app lose it; the client resumes with ``Last-Event-ID``
#: through a fresh authorization.
MAX_STREAM_SECONDS = 3600.0
DEFAULT_PAGE = 100
#: Rows read per store call while filtering; a call scans at most _MAX_BATCHES.
_BATCH = 200
_MAX_BATCHES = 10
_MAX_SEQ = 2**63 - 1  # ``seq`` is a Postgres bigserial
_MAX_PARAM = 512
_SLOTS_STATE = "work_event_stream_slots"


class _BadRequestError(Exception):
    """A malformed query; answered 400 with this code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Query:
    """Who may see what: the store filter plus the checks the store cannot do."""

    agent: str | None
    where: dict[str, str | None]
    kinds: frozenset[str]
    clearance: Classification

    def accepts(self, event: WorkEvent) -> bool:
        if self.kinds and event.kind not in self.kinds:
            return False
        try:
            label = parse_classification(event.classification, strict=True)
        except ValueError:
            return False  # an unknown label is never sent (fail closed)
        return dominates(self.clearance, label)


class _StreamSlots:
    """Open-stream count per principal DID; used from the event loop only."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._open: dict[str, int] = {}

    def acquire(self, did: str) -> bool:
        count = self._open.get(did, 0)
        if count >= self._limit:
            return False
        self._open[did] = count + 1
        return True

    def release(self, did: str) -> None:
        count = self._open.get(did, 0) - 1
        if count > 0:
            self._open[did] = count
        else:
            self._open.pop(did, None)


# --- request parsing -------------------------------------------------------------------


def _param(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    if value is None or value == "":
        return None
    if len(value) > _MAX_PARAM:
        raise _BadRequestError(f"invalid_{name}")
    return value


def _non_negative(raw: str | None, code: str, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _BadRequestError(code) from None
    if not 0 <= value <= _MAX_SEQ:
        raise _BadRequestError(code)
    return value


def _clearance(request: Request) -> Classification:
    """The deployment clearance; a bad label falls to the lowest (fail closed)."""
    label = str(getattr(request.app.state, "inbox_clearance", "UNCLASSIFIED"))
    try:
        return parse_classification(label, strict=True)
    except ValueError:
        return Classification.UNCLASSIFIED


def _query(request: Request, agent: str | None) -> _Query:
    where: dict[str, str | None] = {}
    if agent is not None:
        where["agent_did"] = agent
    for name in ("run_id", "correlation_id"):
        value = _param(request, name)
        if value is not None:
            where[name] = value
    raw_kinds = _param(request, "kinds")
    kinds = frozenset(k.strip() for k in (raw_kinds or "").split(",") if k.strip())
    if len(kinds) == 1:
        where["kind"] = next(iter(kinds))
    return _Query(agent=agent, where=where, kinds=kinds, clearance=_clearance(request))


def _refusal(request: Request, agent: str | None, operation: str) -> JSONResponse | None:
    return require_scope(
        request,
        PrincipalScope.EVENTS_SUBSCRIBE,
        agent_did=agent,
        target=f"work-events:{agent or '*'}",
        operation=operation,
    )


def _log(request: Request) -> WorkEventLog | None:
    log: WorkEventLog | None = getattr(request.app.state, "work_event_log", None)
    return log


async def _matching(
    log: WorkEventLog, after: int, limit: int, query: _Query
) -> tuple[list[WorkEvent], int]:
    """Up to ``limit`` visible events after ``after``, and the seq scanned through."""
    found: list[WorkEvent] = []
    cursor = after
    for _ in range(_MAX_BATCHES):
        batch = await log.read(cursor, limit=_BATCH, where=query.where)
        for event in batch:
            cursor = event.seq or cursor
            if query.accepts(event):
                found.append(event)
                if len(found) == limit:
                    return found, cursor
        if len(batch) < _BATCH:
            break
    return found, cursor


# --- the JSON page and the key ---------------------------------------------------------


async def list_work_events(request: Request) -> Response:
    """GET /api/work-events — visible events after ``after``, ascending by ``seq``."""
    try:
        agent = _param(request, "agent")
        refused = _refusal(request, agent, "work_event.read")
        if refused is not None:
            return refused
        after = _non_negative(request.query_params.get("after"), "invalid_after", 0)
        limit = _non_negative(request.query_params.get("limit"), "invalid_limit", DEFAULT_PAGE)
        query = _query(request, agent)
    except _BadRequestError as exc:
        return JSONResponse({"error": exc.code}, status_code=400)
    log = _log(request)
    if log is None:
        return JSONResponse({"error": "work_events_unavailable"}, status_code=503)
    events, cursor = await _matching(log, after, min(max(limit, 1), MAX_PAGE), query)
    return JSONResponse(
        {
            "events": [pointer_envelope(event, include_seq=True) for event in events],
            "next_after": cursor,
        }
    )


async def work_events_key(request: Request) -> Response:
    """GET /api/work-events/keys — the work-events verify key, or 503 without one."""
    try:
        agent = _param(request, "agent")
    except _BadRequestError as exc:
        return JSONResponse({"error": exc.code}, status_code=400)
    refused = _refusal(request, agent, "work_event.keys")
    if refused is not None:
        return refused
    signer = getattr(request.app.state, "work_event_signer", None)
    if signer is None:
        return JSONResponse({"error": "work_events_key_unavailable"}, status_code=503)
    return JSONResponse(
        {
            "key_id": signer.key_id,
            "algorithm": signer.algorithm,
            "public_key": base64.b64encode(signer.public_key).decode("ascii"),
        }
    )


# --- the stream ------------------------------------------------------------------------


def _frame(event: WorkEvent) -> bytes:
    data = json.dumps(pointer_envelope(event, include_seq=True), separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n".encode()


class _EventStream(Response):
    """Holds a stream slot for exactly as long as the ASGI response runs."""

    def __init__(self, request: Request, log: WorkEventLog, query: _Query, after: int) -> None:
        super().__init__(status_code=200)
        self._request = request
        self._log = log
        self._query = query
        self._after = after
        state = request.app.state
        self._sleep: Callable[[float], Awaitable[None]] = getattr(
            state, "work_events_sleep", asyncio.sleep
        )
        self._clock: Callable[[], float] = getattr(state, "work_events_clock", time.monotonic)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = self._request
        slots = _slots(request)
        did = str(request.state.principal.did)
        target = f"work-events:{self._query.agent or '*'}"
        if not slots.acquire(did):
            emit_mutation_audit(
                request,
                target=target,
                operation="work_event.subscribe",
                outcome="rate_limited",
                detail=f"agent={self._query.agent} open_streams={MAX_STREAMS_PER_PRINCIPAL}",
            )
            limited = JSONResponse({"error": "too_many_streams"}, status_code=429)
            await limited(scope, receive, send)
            return
        detail = f"agent={self._query.agent} kinds={','.join(sorted(self._query.kinds))}"
        frames = self._frames()
        try:
            # Inside the try: a failed audit write must still release the slot.
            emit_mutation_audit(
                request,
                target=target,
                operation="work_event.subscribe",
                outcome="applied",
                detail=detail,
            )
            stream = StreamingResponse(
                frames,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
            await stream(scope, receive, send)
        finally:
            slots.release(did)
            emit_mutation_audit(
                request,
                target=target,
                operation="work_event.close",
                outcome="applied",
                detail=detail,
            )
            await frames.aclose()

    async def _frames(self) -> AsyncGenerator[bytes, None]:
        cursor = self._after
        opened = last_comment = self._clock()
        while self._clock() - opened < MAX_STREAM_SECONDS:
            events, cursor = await _matching(self._log, cursor, _BATCH, self._query)
            for event in events:
                yield _frame(event)
            if self._clock() - last_comment >= HEARTBEAT_SECONDS:
                last_comment = self._clock()
                yield b": keepalive\n\n"
            if not events:
                await self._sleep(POLL_SECONDS)


def _slots(request: Request) -> _StreamSlots:
    """This app's open-stream counter, created on first use."""
    state: Any = request.app.state
    slots: _StreamSlots | None = getattr(state, _SLOTS_STATE, None)
    if slots is None:
        slots = _StreamSlots(MAX_STREAMS_PER_PRINCIPAL)
        setattr(state, _SLOTS_STATE, slots)
    return slots


async def stream_work_events(request: Request) -> Response:
    """GET /api/work-events/stream — the scoped, resumable Server-Sent Events stream."""
    try:
        agent = _param(request, "agent")
        refused = _refusal(request, agent, "work_event.subscribe")
        if refused is not None:
            return refused
        after = _non_negative(request.headers.get("last-event-id"), "invalid_last_event_id", 0)
        query = _query(request, agent)
    except _BadRequestError as exc:
        return JSONResponse({"error": exc.code}, status_code=400)
    log = _log(request)
    if log is None:
        return JSONResponse({"error": "work_events_unavailable"}, status_code=503)
    return _EventStream(request, log, query, after)


routes = [
    Route("/api/work-events/stream", stream_work_events, methods=["GET"]),
    Route("/api/work-events/keys", work_events_key, methods=["GET"]),
    Route("/api/work-events", list_work_events, methods=["GET"]),
]
