"""App principals and person delegations over HTTP (SPEC-087 COMP-011).

``POST /api/me/delegations``              — a signed-in person lets one active app
                                            decide for them (decision scopes only,
                                            at most 8 hours);
``GET  /api/me/delegations``              — the person's own delegations;
``POST /api/me/delegations/{id}/revoke``  — revoke one of them at once.

Consent is personal, not an operator power: any signed-in person (viewer or
operator) manages their own delegations, and only their own. The person is
always the session account, never a body field. Static tokens and app
principals are refused: neither is a person who can consent. Another person's
delegation answers the same 404 as an unknown id, so ids do not leak.

Every write goes through :class:`arctrust.principals.PrincipalStore` (the store
emits ``principal.delegation.*``) off the event loop, and every change, applied
or refused, is one ``ui.mutation`` row. An unwired or unreachable account
authority answers 503 and writes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from arctrust.authority import AccountAuthorityError
from arctrust.authority_config import AuthorityConfigError
from arctrust.monotonic import AnchorUnavailableError
from arctrust.principals import DECISION_SCOPES, Delegation, PrincipalStore, Scope
from arctrust.users import UserStoreError
from arctrust.vault_lease import VaultLeaseError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.principals import RequestPrincipal

logger = logging.getLogger(__name__)

DELEGATIONS_PATH = "/api/me/delegations"
MAX_DELEGATION_TTL_SECONDS = 8 * 60 * 60

_T = TypeVar("_T")
_READ_METHODS = frozenset({"GET", "HEAD"})
_DECISION_SCOPE_VALUES = frozenset(str(scope) for scope in DECISION_SCOPES)

# Every way the account authority can be out of reach (PrincipalStoreError is a
# UserStoreError). The person sees one 503; only the class name is logged.
_AUTHORITY_DOWN = (
    UserStoreError,
    AccountAuthorityError,
    VaultLeaseError,
    AuthorityConfigError,
    AnchorUnavailableError,
)
_UNAVAILABLE = "account_authority_unavailable"


class DelegationCreate(BaseModel):
    """Body of ``POST /api/me/delegations``. Strict: no coercion, no extra fields."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    principal_did: str = Field(min_length=1)
    scopes: list[str] = Field(min_length=1)
    ttl_seconds: int = Field(gt=0, le=MAX_DELEGATION_TTL_SECONDS)


class _RefusedError(Exception):
    """A request the store must not see through; carries the client code and status."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


# --- shared helpers -----------------------------------------------------------


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _audit(request: Request, target: str, operation: str, outcome: str, detail: str) -> None:
    """One ``ui.mutation`` row per change attempt; reads stay off the chain."""
    if request.method in _READ_METHODS:
        return
    emit_mutation_audit(
        request, target=target, operation=operation, outcome=outcome, detail=detail
    )


def _signed_in_person(request: Request) -> str | None:
    """The session account DID, or None for static tokens, apps and anonymous callers."""
    principal: RequestPrincipal | None = getattr(request.state, "principal", None)
    if principal is None or principal.kind != "user":
        return None
    return principal.did


def _refuse_caller(request: Request, operation: str) -> JSONResponse:
    _audit(request, request.url.path, operation, "denied", "not a signed-in person")
    return _error("person_session_required", 403)


async def _in_store(
    request: Request, target: str, operation: str, fn: Callable[[PrincipalStore], _T]
) -> _T | JSONResponse:
    """Run ``fn`` against the principal store off the loop; refusals become responses."""
    factory: Callable[[], PrincipalStore] | None = getattr(
        request.app.state, "principal_store_factory", None
    )
    try:
        if factory is None:
            raise AccountAuthorityError("principal store is not configured")
        return await asyncio.to_thread(lambda: fn(factory()))
    except _RefusedError as refused:
        _audit(request, target, operation, "denied", refused.code)
        return _error(refused.code, refused.status)
    except _AUTHORITY_DOWN as exc:
        logger.error("principals.%s authority_unavailable class=%s", operation, type(exc).__name__)
        _audit(request, target, operation, "error", _UNAVAILABLE)
        return _error(_UNAVAILABLE, 503)


def _delegation_row(delegation: Delegation) -> dict[str, object]:
    return delegation.to_dict()


# --- person delegations ---------------------------------------------------------


async def _parse_create(request: Request) -> DelegationCreate | None:
    try:
        raw: Any = await request.json()
        body = DelegationCreate.model_validate(raw)
    except (ValueError, ValidationError):  # malformed JSON or a refused field
        return None
    if not set(body.scopes) <= _DECISION_SCOPE_VALUES:
        return None
    return body


def _add_for_person(store: PrincipalStore, delegation: Delegation) -> Delegation:
    """Store ``delegation`` only for an active, unexpired app principal."""
    principal = store.get_by_did(delegation.principal_did)
    now = delegation.created_at
    if (
        principal is None
        or principal.status != "active"
        or (principal.expires_at is not None and principal.expires_at <= now)
    ):
        raise _RefusedError("principal_not_active", 400)
    return store.add_delegation(delegation)


def _revoke_own(store: PrincipalStore, person_did: str, delegation_id: str) -> Delegation:
    """Revoke the person's own delegation; anyone else's reads as unknown."""
    own = {d.id: d for d in store.delegations_for(person_did)}
    current = own.get(delegation_id)
    if current is None:
        raise _RefusedError("unknown_delegation", 404)
    if current.revoked:
        raise _RefusedError("already_revoked", 409)
    return store.revoke_delegation(delegation_id, person_did)


async def create_delegation(request: Request) -> JSONResponse:
    """POST /api/me/delegations — the session person delegates decision scopes to an app."""
    operation = "principal.delegation.add"
    person = _signed_in_person(request)
    if person is None:
        return _refuse_caller(request, operation)
    body = await _parse_create(request)
    if body is None:
        _audit(request, DELEGATIONS_PATH, operation, "denied", "invalid_delegation")
        return _error("invalid_delegation", 400)
    now = datetime.now(UTC)
    delegation = Delegation(
        id=uuid.uuid4().hex,
        person_did=person,
        principal_did=body.principal_did,
        scopes=tuple(Scope(value) for value in sorted(set(body.scopes))),
        created_at=now,
        expires_at=now + timedelta(seconds=body.ttl_seconds),
        revoked=False,
    )
    target = f"{DELEGATIONS_PATH}/{delegation.id}"
    result = await _in_store(
        request, target, operation, lambda store: _add_for_person(store, delegation)
    )
    if isinstance(result, JSONResponse):
        return result
    scopes = ",".join(str(scope) for scope in result.scopes)
    _audit(
        request,
        target,
        operation,
        "applied",
        f"app={result.principal_did} scopes={scopes} ttl={body.ttl_seconds}",
    )
    return JSONResponse(_delegation_row(result), status_code=201)


async def list_delegations(request: Request) -> JSONResponse:
    """GET /api/me/delegations — only the session person's own delegations."""
    operation = "principal.delegation.list"
    person = _signed_in_person(request)
    if person is None:
        return _refuse_caller(request, operation)
    result = await _in_store(
        request, DELEGATIONS_PATH, operation, lambda store: store.delegations_for(person)
    )
    if isinstance(result, JSONResponse):
        return result
    rows = [_delegation_row(d) for d in sorted(result, key=lambda d: (d.created_at, d.id))]
    return JSONResponse({"delegations": rows})


async def revoke_delegation(request: Request) -> JSONResponse:
    """POST /api/me/delegations/{id}/revoke — revoke one of the person's own, at once."""
    operation = "principal.delegation.revoke"
    person = _signed_in_person(request)
    if person is None:
        return _refuse_caller(request, operation)
    delegation_id: str = request.path_params["id"]
    target = f"{DELEGATIONS_PATH}/{delegation_id}"
    result = await _in_store(
        request, target, operation, lambda store: _revoke_own(store, person, delegation_id)
    )
    if isinstance(result, JSONResponse):
        return result
    _audit(request, target, operation, "applied", f"app={result.principal_did}")
    return JSONResponse(_delegation_row(result))


routes = [
    Route(DELEGATIONS_PATH, list_delegations, methods=["GET"]),
    Route(DELEGATIONS_PATH, create_delegation, methods=["POST"]),
    Route(DELEGATIONS_PATH + "/{id}/revoke", revoke_delegation, methods=["POST"]),
]

__all__ = [
    "DELEGATIONS_PATH",
    "MAX_DELEGATION_TTL_SECONDS",
    "DelegationCreate",
    "routes",
]
