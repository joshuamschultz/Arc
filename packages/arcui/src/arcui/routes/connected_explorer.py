"""Connected-data EXPLORER routes — read one connection's schema + chunks (H-024).

Two operator-gated, audited GET surfaces bounded to ONE granted connection:

* ``.../connected-sources/{source_id}/tables`` — that connection's introspected
  datastore schema (table names, PK, FKs, searchable columns, row_count), read
  from the PERSISTED ontology via :class:`~arcmemory.operator.MemoryOperator`, so
  it works with the backing datastore unreachable. Schema only — never row
  values, never raw SQL from the UI (row sampling + redaction is H-025's surface).
* ``.../connected-sources/{source_id}/chunks`` — that connection's indexed chunks,
  browsed or (``?q=``) searched through ``MemoryOperator.browse_chunks`` /
  ``search_chunks`` bound to the source's isolated document pool. Every candidate
  is gated no-read-up BEFORE the page slice, so an over-clearance chunk never
  shows and never shifts a count; a ``?mode=vector`` request degrades LOUD.

Four pillars on every read: operator role required (Authorize), the DID half of
every scope is the AUTHENTICATED agent's own (Identity) so a caller-chosen
``source_id`` can never reach another agent's pool, and each read emits a
``ui.connected_data_read`` audit event (Audit). A store failure fails CLOSED as a
503 with the message surfaced verbatim; an ungranted/nonexistent source reads as
an ordinary empty result — it never confirms another agent's source ids.

This module consumes ONLY the public ``MemoryOperator`` facade (no SQL, no store
logic) and reuses the knowledge route's operator builder, mirroring
``routes/knowledge.py``.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_read_audit
from arcui.routes.connected_data import _operator
from arcui.routes.knowledge import (
    _agent_not_found,
    _operator_for,
    _resolve_agent,
    _store_unreadable,
)

#: The UI holds no clearance credential (viewer / operator only), so an explorer
#: read runs at the lowest clearance and the no-read-up gate hides everything
#: above it. Fail-closed by construction: a classified chunk is never exposed.
_UI_CLEARANCE = "unclassified"

#: Bound a single explorer page so one request can never pull a whole corpus.
_MAX_LIMIT = 200


def _int_param(request: Request, name: str, default: int, *, cap: int) -> int:
    """A non-negative int query param, defaulted and capped (never raises on junk)."""
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, min(value, cap))


async def list_connection_tables(request: Request) -> JSONResponse:
    """GET .../connected-sources/{source_id}/tables — one connection's datastore schema."""
    denied = _operator(request)
    if denied is not None:
        return denied
    agent_id = request.path_params["agent_id"]
    source_id = request.path_params["source_id"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    try:
        tables = op.list_datastore_tables(source_id)
    except Exception as exc:
        _emit(request, agent_id, source_id, "connected_data.tables", "error")
        return _store_unreadable(exc)
    _emit(
        request,
        agent_id,
        source_id,
        "connected_data.tables",
        "ok",
        detail=f"tables={len(tables)}",
    )
    return JSONResponse({"items": [t.model_dump(mode="json") for t in tables]})


async def list_connection_chunks(request: Request) -> JSONResponse:
    """GET .../connected-sources/{source_id}/chunks — browse, or ``?q=`` search.

    ``?mode=vector|literal`` selects the search channel (default ``literal``),
    ignored on a plain browse. Both paths bind to the connection's document pool
    and gate every candidate on clearance BEFORE pagination/limit (H-023/H-024).
    """
    denied = _operator(request)
    if denied is not None:
        return denied
    agent_id = request.path_params["agent_id"]
    source_id = request.path_params["source_id"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    query = request.query_params.get("q")
    try:
        if query:
            mode = request.query_params.get("mode", "literal")
            limit = _int_param(request, "limit", 10, cap=_MAX_LIMIT)
            result = await op.search_chunks(
                query, mode=mode, limit=limit, clearance=_UI_CLEARANCE, source_id=source_id
            )
            payload = result.model_dump(mode="json")
            operation = "connected_data.chunks.search"
        else:
            limit = _int_param(request, "limit", 50, cap=_MAX_LIMIT)
            offset = _int_param(request, "offset", 0, cap=1_000_000)
            page = await op.browse_chunks(
                limit=limit, offset=offset, clearance=_UI_CLEARANCE, source_id=source_id
            )
            payload = page.model_dump(mode="json")
            operation = "connected_data.chunks.browse"
    except Exception as exc:
        _emit(request, agent_id, source_id, "connected_data.chunks", "error")
        return _store_unreadable(exc)
    _emit(request, agent_id, source_id, operation, "ok", detail=f"items={len(payload['items'])}")
    return JSONResponse(payload)


def _emit(
    request: Request,
    agent_id: str,
    source_id: str,
    operation: str,
    outcome: str,
    *,
    detail: str = "",
) -> None:
    emit_read_audit(
        request,
        target=f"agent:{agent_id}/source:{source_id}",
        operation=operation,
        outcome=outcome,
        detail=detail,
    )


routes = [
    Route(
        "/api/agents/{agent_id}/knowledge/connected-sources/{source_id}/tables",
        list_connection_tables,
        methods=["GET"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/connected-sources/{source_id}/chunks",
        list_connection_chunks,
        methods=["GET"],
    ),
]

__all__ = ["list_connection_chunks", "list_connection_tables", "routes"]
