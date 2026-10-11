"""Callback allowlist management over HTTP (SPEC-088 COMP-018).

``GET  /api/work-events/callbacks``              — every allowlist entry;
``POST /api/work-events/callbacks``              — allow an origin (body
                                                   ``origin``, ``allow_private``,
                                                   ``clearance``);
``POST /api/work-events/callbacks/{id}/remove``  — remove an entry; callbacks to
                                                   it are refused from the next check.

Only a signed-in operator may call these: static tokens cannot name a person
and app principals are refused by the middleware (these paths have no
``APP_ROUTES`` row). Adding by tier mirrors app principals (D-823): personal is
active at once; enterprise and federal store the entry ``pending`` and queue a
``work_event_callback`` approval row bound to the entry's content hash.
:func:`activate_callback_for_approval` and
:func:`remove_callback_for_denial` are the approvals
route's half of that row.

Every write goes through :class:`arctrust.callback_allowlist.CallbackAllowlistStore`
off the event loop (the store appends its own strict
``work_event.callback_allowlist.*`` audit event first), and every change
attempt, applied or refused, is one ``ui.mutation`` row. An unwired or
unreachable account authority answers 503 and writes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import TypeVar

from arcstore.approvals import ApprovalStore, PendingApproval
from arctrust.authority import AccountAuthorityError
from arctrust.authority_config import AuthorityConfigError
from arctrust.callback_allowlist import (
    CALLBACK_ALLOWLIST_TOOL,
    AllowlistEntry,
    CallbackAllowlistError,
    CallbackAllowlistStore,
    NewCallbackEntry,
)
from arctrust.classification import Classification
from arctrust.monotonic import AnchorUnavailableError
from arctrust.users import OPERATOR, UserStoreError
from arctrust.vault_lease import VaultLeaseError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.principals import RequestPrincipal, Tier

logger = logging.getLogger(__name__)

CALLBACKS_PATH = "/api/work-events/callbacks"

_T = TypeVar("_T")
_OPERATION = "work_event.callback_allowlist"
_UNAVAILABLE = "account_authority_unavailable"

# Store refusals the caller can act on; every other reason (malformed record,
# lock busy, unknown tier) means the authority cannot answer: 503.
_REFUSAL_STATUS = {
    "invalid_origin": 400,
    "duplicate_origin": 409,
    "unknown_entry": 404,
    "already_removed": 409,
}
# Refusals of an approval decision (the record the row names, not the caller's input).
_ACTIVATE_STATUS = {
    "unknown_entry": 409,
    "not_pending": 409,
    "content_hash_mismatch": 409,
    "second_operator_required": 403,
}
# Every way the account authority can be out of reach. CallbackAllowlistError is
# a UserStoreError, so it is caught first and mapped by reason.
_AUTHORITY_DOWN = (
    UserStoreError,
    AccountAuthorityError,
    VaultLeaseError,
    AuthorityConfigError,
    AnchorUnavailableError,
)


class CallbackCreate(BaseModel):
    """Body of ``POST /api/work-events/callbacks``. Strict: no coercion, no extra fields."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    origin: str = Field(min_length=1, max_length=512)
    allow_private: bool = False
    clearance: str = Classification.UNCLASSIFIED.name


class _RefusedError(Exception):
    """A refusal that becomes a client response with this code and status."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


# --- shared helpers -----------------------------------------------------------


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _audit(request: Request, target: str, operation: str, outcome: str, detail: str) -> None:
    """One ``ui.mutation`` row per change attempt; reads stay off the chain."""
    if request.method in {"GET", "HEAD"}:
        return
    emit_mutation_audit(
        request, target=target, operation=operation, outcome=outcome, detail=detail
    )


def _signed_in_operator(request: Request) -> str | None:
    """The session DID of a signed-in operator; None for static tokens, apps and viewers."""
    principal: RequestPrincipal | None = getattr(request.state, "principal", None)
    if principal is None or principal.kind != "user" or OPERATOR not in principal.roles:
        return None
    return principal.did


def _refuse_non_operator(request: Request, operation: str) -> JSONResponse:
    _audit(request, request.url.path, operation, "denied", "not a signed-in operator")
    return _error("operator_session_required", 403)


def _tier(request: Request) -> Tier:
    tier: Tier = request.app.state.principals_config.tier
    return tier


async def _in_store(
    request: Request,
    target: str,
    operation: str,
    fn: Callable[[CallbackAllowlistStore], _T],
    *,
    statuses: Mapping[str, int] | None = None,
) -> _T | JSONResponse:
    """Run ``fn`` against the allowlist store off the loop; refusals become responses."""
    factory: Callable[[], CallbackAllowlistStore] | None = getattr(
        request.app.state, "callback_allowlist_factory", None
    )
    try:
        if factory is None:
            raise AccountAuthorityError("callback allowlist store is not configured")
        return await asyncio.to_thread(lambda: fn(factory()))
    except CallbackAllowlistError as exc:
        status = (_REFUSAL_STATUS if statuses is None else statuses).get(exc.reason)
        if status is not None:
            _audit(request, target, operation, "denied", exc.reason)
            return _error(exc.reason, status)
        logger.error("callbacks.%s authority_refused reason=%s", operation, exc.reason)
        _audit(request, target, operation, "error", _UNAVAILABLE)
        return _error(_UNAVAILABLE, 503)
    except _AUTHORITY_DOWN as exc:
        logger.error("callbacks.%s authority_unavailable class=%s", operation, type(exc).__name__)
        _audit(request, target, operation, "error", _UNAVAILABLE)
        return _error(_UNAVAILABLE, 503)


def _approval_row(entry: AllowlistEntry) -> PendingApproval:
    """The approval row bound to the pending entry's hash; same shape as ``arc callback``."""
    return PendingApproval(
        id=f"cbapprove_{uuid.uuid4().hex[:12]}",
        agent_did=entry.created_by,
        agent_label=entry.origin,
        tool=CALLBACK_ALLOWLIST_TOOL,
        legs=[],
        call_hash=entry.content_hash(),
        arguments={
            "entry_id": entry.id,
            "origin": entry.origin,
            "allow_private": str(entry.allow_private).lower(),
            "clearance": entry.clearance.name,
        },
    )


async def _queue_approval(
    request: Request, entry: AllowlistEntry, target: str
) -> str | JSONResponse:
    """Queue the activation approval; its id, or an audited 503."""
    operation = f"{_OPERATION}.request"
    store: ApprovalStore | None = getattr(request.app.state, "approval_store", None)
    try:
        if store is None:
            raise RuntimeError("approval store is not configured")
        created = await store.create(_approval_row(entry))
    except Exception as exc:  # reason: a failed queue write answers 503, never a 500 page
        # The pending entry stays inert: nothing activates it without this row.
        logger.error("callbacks.request approval_queue_unavailable class=%s", type(exc).__name__)
        _audit(request, target, operation, "error", "approval_queue_unavailable")
        return _error("approval_queue_unavailable", 503)
    return created.id


async def _parse_create(request: Request) -> NewCallbackEntry | None:
    try:
        body = CallbackCreate.model_validate_json(await request.body())
    except ValidationError:  # malformed JSON or a refused field
        return None
    if body.clearance not in Classification.__members__:
        return None
    return NewCallbackEntry(
        origin=body.origin,
        allow_private=body.allow_private,
        clearance=Classification[body.clearance],
    )


# --- routes -------------------------------------------------------------------------


async def list_callbacks(request: Request) -> JSONResponse:
    """GET /api/work-events/callbacks — every entry, oldest first (signed-in operator only)."""
    operation = f"{_OPERATION}.list"
    if _signed_in_operator(request) is None:
        return _refuse_non_operator(request, operation)
    result = await _in_store(request, CALLBACKS_PATH, operation, lambda store: store.list())
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse({"callbacks": [entry.to_dict() for entry in result]})


async def add_callback(request: Request) -> JSONResponse:
    """POST /api/work-events/callbacks — allow an origin; pending above personal tier."""
    operation = f"{_OPERATION}.request"
    operator = _signed_in_operator(request)
    if operator is None:
        return _refuse_non_operator(request, operation)
    new_entry = await _parse_create(request)
    if new_entry is None:
        _audit(request, CALLBACKS_PATH, operation, "denied", "invalid_callback")
        return _error("invalid_callback", 400)
    stored = await _in_store(
        request, CALLBACKS_PATH, operation, lambda store: store.request(new_entry, operator)
    )
    if isinstance(stored, JSONResponse):
        return stored
    target = f"{CALLBACKS_PATH}/{stored.id}"
    body: dict[str, object] = {"callback": stored.to_dict(), "status": stored.status}
    if stored.status == "pending":
        approval_id = await _queue_approval(request, stored, target)
        if isinstance(approval_id, JSONResponse):
            return approval_id
        body["approval_id"] = approval_id
    _audit(
        request,
        target,
        operation,
        "applied",
        f"origin={stored.origin} status={stored.status} allow_private={stored.allow_private}",
    )
    return JSONResponse(body, status_code=201)


async def remove_callback(request: Request) -> JSONResponse:
    """POST /api/work-events/callbacks/{id}/remove — refused from the next callback check."""
    operation = f"{_OPERATION}.remove"
    operator = _signed_in_operator(request)
    if operator is None:
        return _refuse_non_operator(request, operation)
    entry_id: str = request.path_params["id"]
    target = f"{CALLBACKS_PATH}/{entry_id}"
    result = await _in_store(
        request, target, operation, lambda store: store.remove(entry_id, operator)
    )
    if isinstance(result, JSONResponse):
        return result
    _audit(request, target, operation, "applied", f"origin={result.origin}")
    return JSONResponse({"callback": result.to_dict()})


# --- the approvals route's half of a work_event_callback row -------------------------


async def activate_callback_for_approval(
    request: Request, row: PendingApproval, approver_did: str
) -> JSONResponse | None:
    """Activate the entry an approved ``work_event_callback`` row names, or refuse.

    The store activates only a pending entry that still hashes to the row's
    ``call_hash``, and at federal tier only for an approver who is not the
    requester (NIST AC-5). A refusal is one ``approval.approve`` denied row and
    leaves the entry and the approval row as they were.
    """
    entry_id = row.arguments.get("entry_id", "")
    result = await _in_store(
        request,
        f"approval:{row.id}",
        "approval.approve",
        lambda store: store.activate(entry_id, approver_did, row.call_hash),
        statuses=_ACTIVATE_STATUS,
    )
    return result if isinstance(result, JSONResponse) else None


def _remove_pending(store: CallbackAllowlistStore, entry_id: str, actor_did: str) -> None:
    current = {entry.id: entry for entry in store.list()}.get(entry_id)
    if current is not None and current.status == "pending":
        store.remove(entry_id, actor_did)


async def remove_callback_for_denial(
    request: Request, row: PendingApproval, actor_did: str
) -> JSONResponse | None:
    """Remove the pending entry a denied ``work_event_callback`` row names."""
    entry_id = row.arguments.get("entry_id", "")
    result = await _in_store(
        request,
        f"approval:{row.id}",
        "approval.deny",
        lambda store: _remove_pending(store, entry_id, actor_did),
    )
    return result if isinstance(result, JSONResponse) else None


routes = [
    Route(CALLBACKS_PATH, list_callbacks, methods=["GET"]),
    Route(CALLBACKS_PATH, add_callback, methods=["POST"]),
    Route(CALLBACKS_PATH + "/{id}/remove", remove_callback, methods=["POST"]),
]

__all__ = [
    "CALLBACKS_PATH",
    "CallbackCreate",
    "activate_callback_for_approval",
    "remove_callback_for_denial",
    "routes",
]
