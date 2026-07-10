"""Knowledge routes — ``/api/agents/{agent_id}/knowledge/*`` (COMP-002).

REST surface over an agent's memory database, consuming ONLY
``arcmemory.operator.MemoryOperator`` (COMP-001) — this module runs no SQL
and owns no store logic (SDD Overview: "arcui contains zero discovery
logic"). Reads (list/search/detail/links) accept any authenticated role;
mutations (PATCH edit/set-metadata, DELETE) require the operator role and
emit a ``ui.mutation`` audit event through the shared COMP-010 helper.

Empty vs. unreadable (REQ-097): a fresh agent's memory DB is created lazily
on first read by ``MemoryDB.connect()``, so "no memories recorded" is simply
an empty, successful result (200). A genuine store failure (permission
error, corrupted file, blocked path) raises out of the facade call and is
reported as 503 with arcmemory's exception message surfaced verbatim
(REQ-089) — the two states are distinguished by status code, not payload
shape guessing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arcmemory.operator import MemoryOperator, MutationResult, MutationStatus
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

logger = logging.getLogger(__name__)


def _resolve_agent(request: Request, agent_id: str) -> Any | None:
    """Look up a roster entry by id — same lookup agent_detail routes use."""
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            return entry
    return None


def _operator_for(agent_root: Path, agent_did: str) -> MemoryOperator:
    """Build a MemoryOperator over ``<agent_root>/workspace/memory``."""
    return MemoryOperator(agent_root / "workspace", agent_did)


def _agent_not_found(agent_id: str) -> JSONResponse:
    return JSONResponse(
        ErrorResponse(error=f"agent {agent_id!r} not found").model_dump(mode="json"),
        status_code=404,
    )


def _store_unreadable(exc: Exception) -> JSONResponse:
    """REQ-089/REQ-097 — surface the store failure verbatim, distinct from empty."""
    logger.warning("knowledge route: memory store unreadable: %s", exc)
    return JSONResponse(
        ErrorResponse(error=str(exc)).model_dump(mode="json"),
        status_code=503,
    )


def _require_operator(request: Request) -> JSONResponse | None:
    """403 unless the authenticated role is operator. None means proceed."""
    if request.state.role != "operator":
        return JSONResponse(
            ErrorResponse(error="Operator role required").model_dump(mode="json"),
            status_code=403,
        )
    return None


def _mutation_response(entry_id: str, results: list[MutationResult]) -> JSONResponse:
    """Build the PATCH/DELETE response — 200 if every op applied, else 404/500.

    404 specifically for "entry not found" (the facade's stable, code-owned
    error string), so a delete/edit of a missing id reads like a normal REST
    404 rather than a generic failure; any other store error is a 500. The
    payload always carries every sub-result verbatim (REQ-089 — no partial
    success is ever reported as applied).
    """
    overall_applied = all(r.status is MutationStatus.APPLIED for r in results)
    body = {
        "status": "applied" if overall_applied else "error",
        "results": [r.model_dump(mode="json") for r in results],
    }
    if overall_applied:
        return JSONResponse(body, status_code=200)
    not_found = any(r.error is not None and "not found" in r.error for r in results)
    return JSONResponse(body, status_code=404 if not_found else 500)


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


async def list_memories(request: Request) -> JSONResponse:
    """GET .../knowledge/memories — paged list, or ranked search via ``?q=``."""
    agent_id = request.path_params["agent_id"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    query = request.query_params.get("q")

    if query:
        try:
            hits = await op.search(query)
        except Exception as exc:
            return _store_unreadable(exc)
        return JSONResponse({"items": [h.model_dump(mode="json") for h in hits], "query": query})

    limit = int(request.query_params.get("limit", "50"))
    offset = int(request.query_params.get("offset", "0"))
    try:
        page = op.list_entries(limit=limit, offset=offset)
    except Exception as exc:
        return _store_unreadable(exc)
    return JSONResponse(page.model_dump(mode="json"))


async def get_memory(request: Request) -> JSONResponse:
    """GET .../knowledge/memories/{entry_id}."""
    agent_id = request.path_params["agent_id"]
    entry_id = request.path_params["entry_id"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    try:
        record = op.get_entry(entry_id)
    except Exception as exc:
        return _store_unreadable(exc)
    if record is None:
        return JSONResponse(
            ErrorResponse(error=f"entry {entry_id!r} not found").model_dump(mode="json"),
            status_code=404,
        )
    return JSONResponse(record.model_dump(mode="json"))


async def get_memory_links(request: Request) -> JSONResponse:
    """GET .../knowledge/memories/{entry_id}/links (REQ-085)."""
    agent_id = request.path_params["agent_id"]
    entry_id = request.path_params["entry_id"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    try:
        links = op.links(entry_id)
    except Exception as exc:
        return _store_unreadable(exc)
    return JSONResponse({"items": [link.model_dump(mode="json") for link in links]})


async def patch_memory(request: Request) -> JSONResponse:
    """PATCH .../knowledge/memories/{entry_id} — edit text and/or metadata (REQ-088/100)."""
    denied = _require_operator(request)
    if denied is not None:
        return denied

    agent_id = request.path_params["agent_id"]
    entry_id = request.path_params["entry_id"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(
            ErrorResponse(error="Invalid JSON body").model_dump(mode="json"), status_code=400
        )
    text = body.get("text")
    importance = body.get("importance")
    salience = body.get("salience")
    if text is None and importance is None and salience is None:
        return JSONResponse(
            ErrorResponse(
                error="PATCH body must include at least one of: text, importance, salience"
            ).model_dump(mode="json"),
            status_code=400,
        )

    op = _operator_for(Path(agent.workspace_path), agent.did)
    actor_did = getattr(agent, "did", "") or "did:arc:ui:operator"
    results: list[MutationResult] = []
    try:
        if text is not None:
            results.append(op.edit_entry(entry_id, text, actor_did=actor_did))
        if importance is not None or salience is not None:
            results.append(
                op.set_metadata(
                    entry_id, actor_did=actor_did, importance=importance, salience=salience
                )
            )
    except Exception as exc:
        return _store_unreadable(exc)

    outcome = "applied" if all(r.status is MutationStatus.APPLIED for r in results) else "error"
    emit_mutation_audit(
        request,
        target=f"memory://{agent_id}/{entry_id}",
        operation="memory.edit",
        outcome=outcome,
        detail=", ".join(r.operation for r in results),
    )
    return _mutation_response(entry_id, results)


async def delete_memory(request: Request) -> JSONResponse:
    """DELETE .../knowledge/memories/{entry_id} (REQ-088)."""
    denied = _require_operator(request)
    if denied is not None:
        return denied

    agent_id = request.path_params["agent_id"]
    entry_id = request.path_params["entry_id"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    actor_did = getattr(agent, "did", "") or "did:arc:ui:operator"
    try:
        result = op.delete_entry(entry_id, actor_did=actor_did)
    except Exception as exc:
        return _store_unreadable(exc)

    emit_mutation_audit(
        request,
        target=f"memory://{agent_id}/{entry_id}",
        operation="memory.delete",
        outcome=result.status.value,
        detail=result.error or "",
    )
    return _mutation_response(entry_id, [result])


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


async def list_entities(request: Request) -> JSONResponse:
    """GET .../knowledge/entities (REQ-084)."""
    agent_id = request.path_params["agent_id"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    try:
        entities = op.list_entities()
    except Exception as exc:
        return _store_unreadable(exc)
    return JSONResponse({"items": [e.model_dump(mode="json") for e in entities]})


async def get_entity(request: Request) -> JSONResponse:
    """GET .../knowledge/entities/{slug}."""
    agent_id = request.path_params["agent_id"]
    slug = request.path_params["slug"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    try:
        entity = op.get_entity(slug)
    except Exception as exc:
        return _store_unreadable(exc)
    if entity is None:
        return JSONResponse(
            ErrorResponse(error=f"entity {slug!r} not found").model_dump(mode="json"),
            status_code=404,
        )
    return JSONResponse(entity.model_dump(mode="json"))


async def get_entity_links(request: Request) -> JSONResponse:
    """GET .../knowledge/entities/{slug}/links (REQ-085)."""
    agent_id = request.path_params["agent_id"]
    slug = request.path_params["slug"]
    agent = _resolve_agent(request, agent_id)
    if agent is None:
        return _agent_not_found(agent_id)

    op = _operator_for(Path(agent.workspace_path), agent.did)
    try:
        links = op.links(slug)
    except Exception as exc:
        return _store_unreadable(exc)
    return JSONResponse({"items": [link.model_dump(mode="json") for link in links]})


routes = [
    Route("/api/agents/{agent_id}/knowledge/memories", list_memories, methods=["GET"]),
    Route("/api/agents/{agent_id}/knowledge/memories/{entry_id}", get_memory, methods=["GET"]),
    Route(
        "/api/agents/{agent_id}/knowledge/memories/{entry_id}",
        patch_memory,
        methods=["PATCH"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/memories/{entry_id}",
        delete_memory,
        methods=["DELETE"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/memories/{entry_id}/links",
        get_memory_links,
        methods=["GET"],
    ),
    Route("/api/agents/{agent_id}/knowledge/entities", list_entities, methods=["GET"]),
    Route("/api/agents/{agent_id}/knowledge/entities/{slug}", get_entity, methods=["GET"]),
    Route(
        "/api/agents/{agent_id}/knowledge/entities/{slug}/links",
        get_entity_links,
        methods=["GET"],
    ),
]
