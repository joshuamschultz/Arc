"""`/api/connections/{instance}/guide` — the operator's navigation guide for a connection.

Every connection (files, docs, chat, mail, databases) can carry a guide: free
text an operator writes so an agent knows how to navigate the source, like an
``agents.md`` for it. The store is :mod:`arcmemory.source_guide`; this module
is its operator surface.

The guide reaches agents' context, so it is a PROTECTED artifact exactly like
the semantic layer (``routes/semantic_layer.py``): written only by an operator,
secret-scanned, signed through the deployment's operator signer handle (the
seed never enters this module), and verified on every later read. A direct
file edit after signing reads back as ``tampered`` and is never shown to an
agent.

Reads (any authenticated role): the guide and its history. Writes (operator
only): save, restore. The starter draft is operator-only too, because it is
built from the synced contents of the connection. Every call is audited.
"""

from __future__ import annotations

import asyncio
from typing import Any

from arcmemory.semantic_layer import layer_for
from arcmemory.source_guide import (
    MAX_GUIDE_BYTES,
    GuideFacts,
    SourceGuide,
    SourceGuideTooLargeError,
    SourceGuideVersionNotFoundError,
    guide_history,
    guide_path,
    read_guide,
    render_starter_guide,
    restore_guide,
    write_guide,
)
from arctrust.artifact import ArtifactSignature, sign_artifact_with_signer
from arctrust.policy import OperatorApprovalAuthority
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail.files_write import _find_secret
from arcui.routes.trust import operator_signer_for_request
from arcui.schemas import ErrorResponse


class GuideResponse(BaseModel):
    """One connection's guide as the UI shows it."""

    content: str
    signed: bool
    signer: str | None
    updated_at: str | None
    version: int
    tampered: bool


class GuideVersionResponse(BaseModel):
    version: int
    signer: str
    updated_at: str
    digest: str


class GuideHistoryResponse(BaseModel):
    versions: list[GuideVersionResponse]


class GuideStarterResponse(BaseModel):
    content: str


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _shape(guide: SourceGuide) -> JSONResponse:
    return JSONResponse(
        GuideResponse(
            content=guide.content,
            signed=guide.signed,
            signer=guide.signer,
            updated_at=guide.updated_at,
            version=guide.version,
            tampered=guide.tampered,
        ).model_dump(mode="json")
    )


def _instance(request: Request) -> str | None:
    """The connection named in the path, or ``None`` when it is not a usable name."""
    instance = str(request.path_params["instance"])
    return instance if guide_path(instance) is not None else None


def _audit(
    request: Request, instance: str, operation: str, outcome: str, detail: str = ""
) -> None:
    emit_mutation_audit(
        request,
        target=f"source-guide:{instance}",
        operation=f"source_guide.{operation}",
        outcome=outcome,
        detail=detail,
    )


def _denied_unless_operator(
    request: Request, instance: str, operation: str
) -> JSONResponse | None:
    if getattr(request.state, "role", None) == "operator":
        return None
    _audit(request, instance, operation, "denied", "viewer role")
    return _error("operator_role_required", 403)


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # reason: a malformed body is a client error, not a 500
        return None
    return body if isinstance(body, dict) else None


def _signer(request: Request) -> Any:
    """Sign as the deployment operator through its signer handle, never a raw seed."""
    signer = operator_signer_for_request(request)
    did = OperatorApprovalAuthority(signer).did

    def sign(content: bytes) -> ArtifactSignature:
        return sign_artifact_with_signer(content, signer_did=did, signer=signer)

    return sign


# -- reads -------------------------------------------------------------------


async def get_guide(request: Request) -> JSONResponse:
    """GET .../guide — the guide, verified; ``tampered`` when its signature fails."""
    instance = _instance(request)
    if instance is None:
        return _error("not a usable connection name", 400)
    guide = await asyncio.to_thread(read_guide, instance)
    _audit(request, instance, "read", "tampered" if guide.tampered else "applied")
    return _shape(guide)


async def get_history(request: Request) -> JSONResponse:
    """GET .../guide/history — kept versions, newest first."""
    instance = _instance(request)
    if instance is None:
        return _error("not a usable connection name", 400)
    versions = await asyncio.to_thread(guide_history, instance)
    _audit(request, instance, "history", "applied", f"versions={len(versions)}")
    return JSONResponse(
        GuideHistoryResponse(
            versions=[GuideVersionResponse(**v.model_dump()) for v in versions]
        ).model_dump(mode="json")
    )


async def get_starter(request: Request) -> JSONResponse:
    """GET .../guide/starter — a deterministic draft from what Arc already knows."""
    instance = _instance(request)
    if instance is None:
        return _error("not a usable connection name", 400)
    denied = _denied_unless_operator(request, instance, "starter")
    if denied is not None:
        return denied
    facts = await _facts(request, instance)
    _audit(request, instance, "starter", "applied")
    return JSONResponse(
        GuideStarterResponse(content=render_starter_guide(facts)).model_dump(mode="json")
    )


async def _facts(request: Request, instance: str) -> GuideFacts:
    """Facts from the first running agent that holds the connection, plus its tables."""
    layer = await asyncio.to_thread(layer_for, instance)
    tables = sorted(name for name, meaning in layer.table.items() if not meaning.hidden)
    overview = await _overview(request, instance)
    if overview is None:
        return GuideFacts(connection_id=instance, tables=tables)
    return GuideFacts(
        connection_id=instance,
        name=overview.name,
        kind=overview.kind,
        documents=overview.documents,
        folders=list(overview.folders),
        titles=list(overview.titles),
        tables=tables,
    )


async def _overview(request: Request, instance: str) -> Any | None:
    cache = getattr(request.app.state, "embedded_agent_cache", None)
    if cache is None:
        return None
    for agent in cache.values():
        registry = getattr(agent, "_capability_registry", None)
        if registry is None:
            continue
        entry = await registry.get_capability("connected_data")
        service = getattr(getattr(entry, "instance", None), "service", None)
        if service is None:
            continue
        overview = await service.guide_facts(instance)
        if overview is not None:
            return overview
    return None


# -- writes (operator only) --------------------------------------------------


async def put_guide(request: Request) -> JSONResponse:
    """PUT .../guide ``{content}`` — scan, sign as the operator, save a new version."""
    instance = _instance(request)
    if instance is None:
        return _error("not a usable connection name", 400)
    denied = _denied_unless_operator(request, instance, "write")
    if denied is not None:
        return denied
    body = await _json_object(request)
    content = body.get("content") if body is not None else None
    if not isinstance(content, str):
        return _error("expected a JSON body with a string 'content' field", 400)
    if len(content.encode("utf-8")) > MAX_GUIDE_BYTES:
        _audit(request, instance, "write", "denied", "oversize")
        return _error(f"a guide is at most {MAX_GUIDE_BYTES} bytes", 413)
    secret_type = _find_secret(content)
    if secret_type is not None:
        _audit(request, instance, "write", "denied", f"secret_content:{secret_type}")
        return _error(
            f"Refusing to save the guide for {instance!r}: it looks like a live credential "
            f"({secret_type}). Credentials never touch the filesystem.",
            400,
        )
    return await _signed(
        request, instance, "write", lambda sign: write_guide(instance, content, sign)
    )


async def restore(request: Request) -> JSONResponse:
    """POST .../guide/restore ``{version}`` — re-sign a kept version as the newest."""
    instance = _instance(request)
    if instance is None:
        return _error("not a usable connection name", 400)
    denied = _denied_unless_operator(request, instance, "restore")
    if denied is not None:
        return denied
    body = await _json_object(request)
    version = body.get("version") if body is not None else None
    if not isinstance(version, int) or isinstance(version, bool):
        return _error("expected a JSON body with an integer 'version' field", 400)
    return await _signed(
        request, instance, "restore", lambda sign: restore_guide(instance, version, sign)
    )


async def _signed(request: Request, instance: str, operation: str, save: Any) -> JSONResponse:
    """Resolve the operator signer, run ``save`` with it off the loop, audit the outcome."""
    try:
        sign = _signer(request)
    except Exception as exc:  # reason: no signing authority -> refuse, never write unsigned
        _audit(request, instance, operation, "error", type(exc).__name__)
        return _error("cannot sign the guide: operator signing authority unavailable", 500)
    try:
        guide: SourceGuide = await asyncio.to_thread(save, sign)
    except SourceGuideVersionNotFoundError as exc:
        _audit(request, instance, operation, "denied", "unknown_version")
        return _error(str(exc), 404)
    except SourceGuideTooLargeError as exc:
        _audit(request, instance, operation, "denied", "oversize")
        return _error(str(exc), 413)
    except RuntimeError as exc:  # reason: SourceGuideTamperedError — refuse to re-sign
        _audit(request, instance, operation, "denied", "tampered_version")
        return _error(str(exc), 409)
    except OSError as exc:
        _audit(request, instance, operation, "error", type(exc).__name__)
        return _error(f"could not write the guide: {exc}", 500)
    _audit(
        request,
        instance,
        operation,
        "applied",
        f"version={guide.version} signer={guide.signer} sha256={guide.digest}",
    )
    return _shape(guide)


routes = [
    Route("/api/connections/{instance}/guide", get_guide, methods=["GET"]),
    Route("/api/connections/{instance}/guide", put_guide, methods=["PUT"]),
    Route("/api/connections/{instance}/guide/history", get_history, methods=["GET"]),
    Route("/api/connections/{instance}/guide/restore", restore, methods=["POST"]),
    Route("/api/connections/{instance}/guide/starter", get_starter, methods=["GET"]),
]

__all__ = ["get_guide", "get_history", "get_starter", "put_guide", "restore", "routes"]
