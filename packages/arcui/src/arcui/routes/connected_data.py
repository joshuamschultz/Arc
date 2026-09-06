"""Connected-data operator routes over ArcAgent's public capability service.

The UI never discovers source internals or writes a mapping itself. The
connected-data module supplies a safe source inventory and stages hash-bound
mapping approvals; ArcUI's existing ``/api/approvals`` route is the only path
that can approve or deny them with the deployment operator key.
"""

from __future__ import annotations

from typing import Any

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_did
from arcui.schemas import ConnectedDataActivationResponse, ErrorResponse

_HOMES = frozenset({"document", "memory", "profile", "blob", "datastore"})


def _refused(error: Any) -> JSONResponse:
    """A source that understood the request and said no.

    Retrying changes nothing, so this is the operator's to fix and the adapter's
    own message is the instruction — "select one folder" must reach the person
    clicking, not be flattened into a generic outage.
    """
    return JSONResponse(
        ErrorResponse(error=str(error)).model_dump(),
        status_code=400,
    )


def _unreachable(source_id: str) -> JSONResponse:
    """A provider that would not answer is a 503 an operator can act on.

    Reading a source runs third-party code against someone else's service. Left
    to propagate, an expired token or a read timeout reached the browser as an
    opaque 500 on the mapping screen, with the remedy — retry, or reconnect the
    account — nowhere in sight.
    """
    return JSONResponse(
        ErrorResponse(
            error=f"source {source_id} could not be read — the provider did not answer. "
            "Retry, or reconnect the account if it keeps failing."
        ).model_dump(),
        status_code=503,
    )


def _agent(request: Request, agent_id: str) -> Any | None:
    cache = getattr(request.app.state, "embedded_agent_cache", None)
    did = _agent_did(request, agent_id)
    return cache.get(did) if cache is not None and did is not None else None


async def _service(request: Request, agent_id: str) -> Any | None:
    """Resolve only the optional public capability service, never its internals."""
    agent = _agent(request, agent_id)
    registry = getattr(agent, "_capability_registry", None)
    if registry is None:
        return None
    entry = await registry.get_capability("connected_data")
    return getattr(getattr(entry, "instance", None), "service", None)


async def _connection_id(service: Any, identifier: str) -> str | None:
    """Map a wire source identifier to the connection_id the service acts on.

    The status projection exposes a canonical ``source_id`` (a hash, once a source
    has synced) alongside its ``connection_id``, but every service action keys on
    the connection_id. A surface that sent the ``source_id`` back — which the card
    does — hit "source not found" on every mapping, resource and sync action the
    moment a source had synced once. Accept either form.
    """
    for status in await service.list_sources():
        if identifier in (
            str(getattr(status, "source_id", "") or ""),
            str(getattr(status, "connection_id", "") or ""),
        ):
            return str(status.connection_id)
    return None


def _operator(request: Request) -> JSONResponse | None:
    if getattr(request.state, "role", None) != "operator":
        return JSONResponse(
            ErrorResponse(error="Operator role required").model_dump(), status_code=403
        )
    return None


def _value(item: Any, name: str, default: Any = None) -> Any:
    """Read a public status model without serializing credentials or cursors."""
    return getattr(item, name, default)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _status_wire(status: Any) -> dict[str, Any]:
    """Return the intentionally small, safe operational source projection."""
    description = _value(status, "description")
    state = _value(status, "state")
    connection_id = str(_value(status, "connection_id", ""))
    source_id = str(_value(status, "source_id", connection_id))
    display_name = _value(status, "label", None) or _value(description, "display_name", "")
    source_kind = _value(status, "source_kind", None) or _value(description, "source_kind", "")
    return {
        "connection_id": connection_id,
        "source_id": source_id,
        "label": str(display_name or connection_id),
        "source_kind": str(source_kind or "unknown"),
        "status": str(_value(status, "status", "idle")),
        "detail": str(_value(status, "detail", "")),
        "pages": int(_value(state, "pages", _value(status, "pages", 0)) or 0),
        "bytes_processed": int(
            _value(state, "bytes_processed", _value(status, "bytes_processed", 0)) or 0
        ),
        "error_code": _value(state, "error_code", _value(status, "error_code", None)),
        # The last successful-sync time is stamped on the durable sync state, not
        # the transient runtime status — reading it off the status alone was
        # always None, so the card read "Never" even for a source that had synced.
        "last_synced_at": _iso(
            _value(state, "last_synced_at", _value(status, "last_synced_at", None))
        ),
        "documents_indexed": int(_value(status, "documents_indexed", 0) or 0),
        "allowed_homes": [str(home) for home in _value(status, "allowed_homes", ())],
    }


def _proposal_wire(proposal: Any) -> dict[str, Any] | None:
    if proposal is None:
        return None
    return {
        "source_id": str(_value(proposal, "source_id", "")),
        "homes": [str(home) for home in _value(proposal, "homes", ())],
        "status": str(_value(proposal, "approval_status", "not_staged")),
        "approval_id": _value(proposal, "approval_id", None),
        "detail": str(_value(proposal, "detail", "")),
        "allowed_homes": [str(home) for home in _value(proposal, "allowed_homes", ())],
    }


async def connected_sources(request: Request) -> JSONResponse:
    """List every account immediately after it is connected, even pre-ingest."""
    service = await _service(request, request.path_params["agent_id"])
    if service is None:
        return JSONResponse({"items": [], "status": "degraded"})
    return JSONResponse({"items": [_status_wire(item) for item in await service.list_sources()]})


async def activate_connected_data(request: Request) -> JSONResponse:
    """Enable the installed Knowledge module for one existing agent.

    This is deliberately an ArcUI operator action over ArcAgent's public
    hot-swap seam. It never writes an agent TOML file or reaches into module
    runtime state; the agent owns activation, persistence, teardown and reload.
    """
    denied = _operator(request)
    if denied is not None:
        return denied
    agent_id = request.path_params["agent_id"]
    agent = _agent(request, agent_id)
    activate = getattr(agent, "enable_module_persisted", None)
    if agent is None or not callable(activate):
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}",
            operation="connected_data.activate",
            outcome="not_found",
        )
        return JSONResponse(
            ErrorResponse(error="agent is not available").model_dump(), status_code=404
        )
    try:
        detail = await activate("connected_data")
    except Exception:  # Public module activation must never leak runtime topology to the browser.
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}",
            operation="connected_data.activate",
            outcome="error",
        )
        return JSONResponse(
            ErrorResponse(error="connected-data module could not be activated").model_dump(),
            status_code=503,
        )
    emit_mutation_audit(
        request,
        target=f"agent:{agent_id}",
        operation="connected_data.activate",
        outcome="applied",
    )
    return JSONResponse(
        ConnectedDataActivationResponse(status="activated", detail=str(detail)).model_dump()
    )


async def sync_status(request: Request) -> JSONResponse:
    """Compatibility projection for existing operational status consumers."""
    service = await _service(request, request.path_params["agent_id"])
    if service is None:
        return JSONResponse({"items": [], "status": "degraded"})
    return JSONResponse({"items": [_status_wire(item) for item in await service.list_sources()]})


async def get_mapping_proposal(request: Request) -> JSONResponse:
    """Read the current mapping proposal and its approval state."""
    service = await _service(request, request.path_params["agent_id"])
    if service is None:
        return JSONResponse({"item": None, "status": "degraded"})
    source_id = request.path_params["source_id"]
    connection_id = await _connection_id(service, source_id)
    if connection_id is None:
        return JSONResponse({"item": None})
    try:
        proposal = await service.get_mapping_proposal(connection_id)
    except arcagent.SourceRefusedError as refusal:
        return _refused(refusal)
    except arcagent.SourceUnreachableError:
        return _unreachable(source_id)
    return JSONResponse({"item": _proposal_wire(proposal)})


async def stage_mapping(request: Request) -> JSONResponse:
    """Stage a mapping; its approval is resolved through the signed generic route."""
    denied = _operator(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(ErrorResponse(error="Invalid JSON body").model_dump(), status_code=400)
    homes = body.get("homes") if isinstance(body, dict) else None
    if (
        not isinstance(homes, list)
        or not homes
        or any(not isinstance(home, str) or home not in _HOMES for home in homes)
        or len(set(homes)) != len(homes)
    ):
        return JSONResponse(
            ErrorResponse(
                error="homes must be a non-empty unique list of allowed data homes"
            ).model_dump(),
            status_code=400,
        )
    agent_id = request.path_params["agent_id"]
    source_id = request.path_params["source_id"]
    service = await _service(request, agent_id)
    if service is None:
        return JSONResponse(
            ErrorResponse(error="connected-data module unavailable").model_dump(), status_code=503
        )
    connection_id = await _connection_id(service, source_id)
    if connection_id is None:
        return JSONResponse(ErrorResponse(error="source not found").model_dump(), status_code=404)
    try:
        proposal = await service.stage_mapping(connection_id, homes=tuple(homes))
    except arcagent.SourceRefusedError as refusal:
        return _refused(refusal)
    except arcagent.SourceUnreachableError:
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}/source:{source_id}",
            operation="connected_data.stage_mapping",
            outcome="error",
            detail="source_unreachable",
        )
        return _unreachable(source_id)
    wire = _proposal_wire(proposal)
    emit_mutation_audit(
        request,
        target=f"agent:{agent_id}/source:{source_id}",
        operation="connected_data.stage_mapping",
        outcome="applied" if wire is not None else "error",
        detail=",".join(homes),
    )
    return JSONResponse({"item": wire})


def _resource_wire(resource: Any) -> dict[str, Any]:
    return {
        "resource_id": str(_value(resource, "resource_id", "")),
        "label": str(_value(resource, "label", "")),
        "resource_kind": str(_value(resource, "resource_kind", "")),
        "selected": bool(_value(resource, "selected", False)),
        "detail": str(_value(resource, "detail", "")),
    }


def _review_wire(review: Any) -> dict[str, Any]:
    provenance = _value(review, "provenance")
    return {
        "fact_id": str(_value(review, "fact_id", "")),
        "profile_id": str(_value(review, "profile_id", "")),
        "field": str(_value(review, "field", "")),
        "value": str(_value(review, "value", "")),
        "kind": str(_value(review, "kind", "")),
        "status": str(_value(review, "status", "")),
        "classification": str(_value(review, "classification", "unclassified")),
        "source_id": str(_value(provenance, "source", "")),
        "external_id": str(_value(provenance, "external_id", "")),
        "replaces_fact_id": _value(review, "replaces_fact_id", None),
    }


async def list_profile_reviews(request: Request) -> JSONResponse:
    """List reviewable profile candidates; this is an operator-only surface."""
    denied = _operator(request)
    if denied is not None:
        return denied
    service = await _service(request, request.path_params["agent_id"])
    if service is None:
        return JSONResponse({"items": []})
    status = request.query_params.get("status")
    source_id = request.query_params.get("source_id")
    items = await service.list_review_items(status=status or None, source_id=source_id or None)
    return JSONResponse({"items": [_review_wire(item) for item in items]})


async def resolve_profile_review(request: Request) -> JSONResponse:
    """Approve, decline, or undo one profile fact through the runtime review seam."""
    denied = _operator(request)
    if denied is not None:
        return denied
    agent_id = request.path_params["agent_id"]
    review_id = request.path_params["review_id"]
    decision = request.path_params["decision"]
    if decision not in {"approve", "decline", "undo"}:
        return JSONResponse(
            ErrorResponse(error="unsupported review decision").model_dump(), status_code=400
        )
    service = await _service(request, agent_id)
    if service is None:
        return JSONResponse(
            ErrorResponse(error="connected-data module unavailable").model_dump(), status_code=503
        )
    item = await service.resolve_review(review_id, decision)
    if item is None:
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}/profile-review:{review_id}",
            operation=f"connected_data.profile_review.{decision}",
            outcome="not_found",
        )
        return JSONResponse(
            ErrorResponse(error="review item not found or not actionable").model_dump(),
            status_code=404,
        )
    emit_mutation_audit(
        request,
        target=f"agent:{agent_id}/profile-review:{review_id}",
        operation=f"connected_data.profile_review.{decision}",
        outcome="applied",
    )
    return JSONResponse(_review_wire(item))


async def list_resources(request: Request) -> JSONResponse:
    """List selectable source containers without reading their content."""
    service = await _service(request, request.path_params["agent_id"])
    if service is None:
        return JSONResponse({"items": []})
    source_id = request.path_params["source_id"]
    connection_id = await _connection_id(service, source_id)
    if connection_id is None:
        return JSONResponse({"items": []})
    try:
        items = await service.list_resources(connection_id)
    except arcagent.SourceRefusedError as refusal:
        return _refused(refusal)
    except arcagent.SourceUnreachableError:
        return _unreachable(source_id)
    return JSONResponse({"items": [_resource_wire(item) for item in items]})


async def select_resources(request: Request) -> JSONResponse:
    """Persist the operator's source scope before synchronization begins."""
    denied = _operator(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(ErrorResponse(error="Invalid JSON body").model_dump(), status_code=400)
    resource_ids = body.get("resource_ids") if isinstance(body, dict) else None
    if (
        not isinstance(resource_ids, list)
        or not resource_ids
        or len(resource_ids) > 1000
        or any(not isinstance(item, str) or not item or len(item) > 512 for item in resource_ids)
        or len(set(resource_ids)) != len(resource_ids)
    ):
        return JSONResponse(
            ErrorResponse(
                error="resource_ids must be a non-empty unique list of at most 1000 ids"
            ).model_dump(),
            status_code=400,
        )
    agent_id = request.path_params["agent_id"]
    source_id = request.path_params["source_id"]
    service = await _service(request, agent_id)
    if service is None:
        return JSONResponse(
            ErrorResponse(error="connected-data module unavailable").model_dump(), status_code=503
        )
    connection_id = await _connection_id(service, source_id)
    if connection_id is None:
        return JSONResponse(ErrorResponse(error="source not found").model_dump(), status_code=404)
    try:
        resources = await service.select_resources(connection_id, resource_ids=tuple(resource_ids))
    except arcagent.SourceRefusedError as refusal:
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}/source:{source_id}",
            operation="connected_data.select_resources",
            outcome="error",
            detail="source_refused",
        )
        return _refused(refusal)
    except arcagent.SourceUnreachableError:
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}/source:{source_id}",
            operation="connected_data.select_resources",
            outcome="error",
            detail="source_unreachable",
        )
        return _unreachable(source_id)
    emit_mutation_audit(
        request,
        target=f"agent:{agent_id}/source:{source_id}",
        operation="connected_data.select_resources",
        outcome="applied",
        detail=f"resources={len(resource_ids)}",
    )
    return JSONResponse({"items": [_resource_wire(item) for item in resources]})


async def sync_action(request: Request) -> JSONResponse:
    """Run one lifecycle action through the service's bounded scheduler."""
    denied = _operator(request)
    if denied is not None:
        return denied
    agent_id = request.path_params["agent_id"]
    source_id = request.path_params["source_id"]
    action = request.path_params["action"]
    service = await _service(request, agent_id)
    if service is None:
        return JSONResponse(
            ErrorResponse(error="connected-data module unavailable").model_dump(), status_code=503
        )
    operations = {
        "sync": service.sync_now,
        "retry": service.sync_now,
        "pause": service.pause,
        "resume": service.resume,
        "reindex": service.reindex,
        "revoke": service.revoke,
    }
    operation = operations.get(action)
    if operation is None:
        return JSONResponse(
            ErrorResponse(error="unsupported sync action").model_dump(), status_code=400
        )
    connection_id = await _connection_id(service, source_id)
    if connection_id is None:
        return JSONResponse(ErrorResponse(error="source not found").model_dump(), status_code=404)
    result = await operation(connection_id)
    result_status = getattr(result, "status", "scheduled" if result else "not_found")
    applied = result is True or result_status in {"scheduled", "paused", "revoked"}
    emit_mutation_audit(
        request,
        target=f"agent:{agent_id}/source:{source_id}",
        operation=f"connected_data.{action}",
        outcome="applied" if applied else "not_found",
    )
    if result_status == "not_found":
        return JSONResponse(ErrorResponse(error="source not found").model_dump(), status_code=404)
    if not applied:
        return JSONResponse(
            ErrorResponse(error=getattr(result, "detail", "source action refused")).model_dump(),
            status_code=409,
        )
    return JSONResponse({"status": result_status, "action": action, "source_id": source_id})


routes = [
    Route(
        "/api/agents/{agent_id}/knowledge/connected-data/activate",
        activate_connected_data,
        methods=["POST"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/connected-sources",
        connected_sources,
        methods=["GET"],
    ),
    Route("/api/agents/{agent_id}/knowledge/sync", sync_status, methods=["GET"]),
    Route(
        "/api/agents/{agent_id}/knowledge/connected-sources/{source_id}/mapping",
        get_mapping_proposal,
        methods=["GET"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/connected-sources/{source_id}/mapping",
        stage_mapping,
        methods=["POST"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/connected-sources/{source_id}/resources",
        list_resources,
        methods=["GET"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/connected-sources/{source_id}/resources",
        select_resources,
        methods=["POST"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/profile-reviews",
        list_profile_reviews,
        methods=["GET"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/profile-reviews/{review_id}/{decision}",
        resolve_profile_review,
        methods=["POST"],
    ),
    Route(
        "/api/agents/{agent_id}/knowledge/sync/{source_id}/{action}",
        sync_action,
        methods=["POST"],
    ),
]

__all__ = [
    "activate_connected_data",
    "connected_sources",
    "get_mapping_proposal",
    "list_profile_reviews",
    "list_resources",
    "resolve_profile_review",
    "routes",
    "select_resources",
    "stage_mapping",
    "sync_action",
    "sync_status",
]
