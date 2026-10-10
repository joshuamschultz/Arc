"""App principals and person delegations over HTTP (SPEC-087 COMP-010, COMP-011).

``POST /api/principals``                  — a signed-in operator issues an app
                                            principal; the bearer secret is in
                                            this response and nowhere else;
``GET  /api/principals``                  — every principal, never a secret;
``POST /api/principals/{id}/revoke``      — revoke it; the next request fails.

Issuing by tier (D-823): personal is active at once. Enterprise and federal
store the principal ``pending`` and queue a ``principal_issue`` approval row
bound to the record's content hash; :func:`activate_for_approval` and
:func:`revoke_for_denial` are the approvals route's half of that row.

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
import hmac
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypeVar

from arcstore.approvals import ApprovalStore, PendingApproval
from arctrust.authority import AccountAuthorityError
from arctrust.authority_config import AuthorityConfigError
from arctrust.canonical import canonical_json
from arctrust.identity import parse_did
from arctrust.monotonic import AnchorUnavailableError
from arctrust.principal_auth import format_bearer, issue_secret
from arctrust.principals import (
    DECISION_SCOPES,
    PRINCIPAL_ISSUE_TOOL,
    Delegation,
    Principal,
    PrincipalStore,
    PrincipalValidationError,
    Scope,
    Tier,
    derive_app_did,
    parse_scope,
    validate_for_tier,
)
from arctrust.users import OPERATOR, UserStoreError
from arctrust.vault_lease import VaultLeaseError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.principals import RequestPrincipal

logger = logging.getLogger(__name__)

DELEGATIONS_PATH = "/api/me/delegations"
PRINCIPALS_PATH = "/api/principals"
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


_AgentList = Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]


class PrincipalIssue(BaseModel):
    """Body of ``POST /api/principals``. Strict: no coercion, no extra fields."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1, max_length=64)
    public_key: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    scopes: dict[str, _AgentList] = Field(min_length=1)
    subjects: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    expires_at: AwareDatetime | None = None


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


# --- app principals (COMP-010) -------------------------------------------------------


def _signed_in_operator(request: Request) -> str | None:
    """The session DID of a signed-in operator; None for anyone else.

    Static tokens are refused even with the operator role: issuing an app
    credential must name the person who did it.
    """
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


def _principal_row(principal: Principal) -> dict[str, object]:
    """The operator view of a principal: never the secret, never its digest."""
    row = principal.to_dict()
    del row["secret_digest"]
    row["agents"] = sorted({agent for agents in principal.scopes.values() for agent in agents})
    return row


def _new_principal(body: PrincipalIssue, operator_did: str, digest: str) -> Principal:
    """The pending principal the body describes; scope names are checked here."""
    public_key = body.public_key.lower()
    # The app is minted in its issuer's org, so the two DIDs share one tenant.
    org = parse_did(operator_did)["org"]
    scopes = {
        parse_scope(name): tuple(sorted(set(agents))) for name, agents in body.scopes.items()
    }
    return Principal(
        id=uuid.uuid4().hex,
        name=body.name,
        did=derive_app_did(bytes.fromhex(public_key), org=org),
        public_key=public_key,
        secret_digest=digest,
        scopes=scopes,
        subjects=tuple(sorted(set(body.subjects))),
        status="pending",
        created_by=operator_did,
        approved_by=None,
        created_at=datetime.now(UTC),
        expires_at=body.expires_at,
        last_used_at=None,
        revoked_by=None,
        revoked_at=None,
    )


async def _parse_issue(
    request: Request, operator_did: str, digest: str
) -> Principal | JSONResponse:
    """The validated pending principal, or the audited 400 refusal."""
    operation = "principal.issue"
    try:
        body = PrincipalIssue.model_validate_json(await request.body())
    except ValidationError:  # malformed JSON or a refused field
        _audit(request, PRINCIPALS_PATH, operation, "denied", "invalid_principal")
        return _error("invalid_principal", 400)
    try:
        principal = _new_principal(body, operator_did, digest)
        validate_for_tier(principal, _tier(request), now=principal.created_at)
    except PrincipalValidationError as exc:
        _audit(request, PRINCIPALS_PATH, operation, "denied", exc.reason)
        return _error(exc.reason, 400)
    return principal


def _store_issued(store: PrincipalStore, principal: Principal, tier: Tier) -> Principal:
    """Store the request; personal activates it in the same call (D-823)."""
    if store.get_by_did(principal.did) is not None:
        raise _RefusedError("duplicate_principal", 409)
    store.request(principal)
    if tier != "personal":
        return principal
    return store.activate(principal.id, principal.created_by, principal.content_hash())


def _issue_approval_row(principal: Principal) -> PendingApproval:
    """The approval row bound to the pending record's content hash."""
    scopes = {str(scope): sorted(agents) for scope, agents in principal.scopes.items()}
    first_agent = principal.scopes[min(principal.scopes)][0]
    return PendingApproval(
        id=f"prinissue_{uuid.uuid4().hex[:12]}",
        agent_did=first_agent,
        agent_label=principal.name,
        tool=PRINCIPAL_ISSUE_TOOL,
        call_hash=principal.content_hash(),
        arguments={
            "principal_id": principal.id,
            "name": principal.name,
            "scopes": canonical_json(scopes).decode("utf-8"),
        },
    )


async def _queue_approval(
    request: Request, principal: Principal, target: str
) -> str | JSONResponse:
    """Queue the issue approval; its id, or an audited 503."""
    store: ApprovalStore | None = getattr(request.app.state, "approval_store", None)
    try:
        if store is None:
            raise RuntimeError("approval store is not configured")
        created = await store.create(_issue_approval_row(principal))
    except Exception as exc:  # reason: a failed queue write answers 503, never a 500 page
        # The pending record stays inert: nothing activates it without this row.
        logger.error("principals.issue approval_queue_unavailable class=%s", type(exc).__name__)
        _audit(request, target, "principal.issue", "error", "approval_queue_unavailable")
        return _error("approval_queue_unavailable", 503)
    return created.id


async def issue_principal(request: Request) -> JSONResponse:
    """POST /api/principals — issue an app principal; the secret is returned once."""
    operation = "principal.issue"
    operator = _signed_in_operator(request)
    if operator is None:
        return _refuse_non_operator(request, operation)
    secret, digest = issue_secret()
    parsed = await _parse_issue(request, operator, digest)
    if isinstance(parsed, JSONResponse):
        return parsed
    target = f"{PRINCIPALS_PATH}/{parsed.id}"
    tier = _tier(request)
    stored = await _in_store(
        request, target, operation, lambda store: _store_issued(store, parsed, tier)
    )
    if isinstance(stored, JSONResponse):
        return stored
    body: dict[str, object] = {
        "principal": _principal_row(stored),
        "secret": format_bearer(stored.id, secret),
        "status": stored.status,
    }
    if stored.status == "pending":
        approval_id = await _queue_approval(request, stored, target)
        if isinstance(approval_id, JSONResponse):
            return approval_id
        body["approval_id"] = approval_id
    _audit(request, target, operation, "applied", f"did={stored.did} status={stored.status}")
    return JSONResponse(body, status_code=201)


async def _pending_approval_ids(request: Request, principals: list[Principal]) -> dict[str, str]:
    """principal id -> its pending ``principal_issue`` approval id; O(pending rows).

    A missing or unreachable approval queue leaves the map empty: the row still
    lists as pending, only the link to its approval card is absent.
    """
    if not any(p.status == "pending" for p in principals):
        return {}
    store: ApprovalStore | None = getattr(request.app.state, "approval_store", None)
    if store is None:
        return {}
    try:
        rows = await store.list(status="pending")
    except Exception as exc:  # reason: the link is optional; the list must still answer
        logger.warning("principals.list approval_lookup_unavailable class=%s", type(exc).__name__)
        return {}
    return {
        row.arguments["principal_id"]: row.id
        for row in rows
        if row.tool == PRINCIPAL_ISSUE_TOOL and row.arguments.get("principal_id")
    }


async def list_principals(request: Request) -> JSONResponse:
    """GET /api/principals — every principal (signed-in operator only).

    A pending row carries ``approval_id`` so the operator can open its approval card.
    """
    operation = "principal.list"
    if _signed_in_operator(request) is None:
        return _refuse_non_operator(request, operation)
    result = await _in_store(request, PRINCIPALS_PATH, operation, lambda store: store.list())
    if isinstance(result, JSONResponse):
        return result
    approval_ids = await _pending_approval_ids(request, result)
    rows = []
    for principal in result:
        row = _principal_row(principal)
        if principal.status == "pending" and principal.id in approval_ids:
            row["approval_id"] = approval_ids[principal.id]
        rows.append(row)
    return JSONResponse({"principals": rows})


def _revoke_known(store: PrincipalStore, principal_id: str, actor_did: str) -> Principal:
    current = store.get(principal_id)
    if current is None:
        raise _RefusedError("unknown_principal", 404)
    if current.status == "revoked":
        raise _RefusedError("already_revoked", 409)
    return store.revoke(principal_id, actor_did)


async def revoke_principal(request: Request) -> JSONResponse:
    """POST /api/principals/{id}/revoke — the app is refused on its next request."""
    operation = "principal.revoke"
    operator = _signed_in_operator(request)
    if operator is None:
        return _refuse_non_operator(request, operation)
    principal_id: str = request.path_params["id"]
    target = f"{PRINCIPALS_PATH}/{principal_id}"
    result = await _in_store(
        request, target, operation, lambda store: _revoke_known(store, principal_id, operator)
    )
    if isinstance(result, JSONResponse):
        return result
    _audit(request, target, operation, "applied", f"did={result.did}")
    return JSONResponse({"principal": _principal_row(result)})


# --- the approvals route's half of a principal_issue row -----------------------------


def _activate_requested(
    store: PrincipalStore, row: PendingApproval, approver_did: str, *, two_person: bool
) -> Principal:
    """Re-read the record the row names; activate it only if it is unchanged."""
    principal_id = row.arguments.get("principal_id", "")
    record = store.get(principal_id) if principal_id else None
    if record is None:
        raise _RefusedError("principal_issue_record_missing", 409)
    if record.status != "pending":
        raise _RefusedError("principal_not_pending", 409)
    if two_person and record.created_by == approver_did:
        raise _RefusedError("second_operator_required", 403)
    if not hmac.compare_digest(record.content_hash().encode(), row.call_hash.encode()):
        raise _RefusedError("principal_changed_since_the_request", 409)
    return store.activate(principal_id, approver_did, row.call_hash)


async def activate_for_approval(
    request: Request, row: PendingApproval, approver_did: str
) -> JSONResponse | None:
    """Activate the principal an approved ``principal_issue`` row names, or refuse.

    A refusal (missing record, not pending, federal self-approval, changed record)
    is one ``approval.approve`` denied row and leaves the record and the row as
    they were. Federal needs a second operator (REQ-554, NIST AC-5).
    """
    two_person = _tier(request) == "federal"
    result = await _in_store(
        request,
        f"approval:{row.id}",
        "approval.approve",
        lambda store: _activate_requested(store, row, approver_did, two_person=two_person),
    )
    return result if isinstance(result, JSONResponse) else None


def _revoke_requested(store: PrincipalStore, principal_id: str, actor_did: str) -> None:
    record = store.get(principal_id) if principal_id else None
    if record is not None and record.status != "revoked":
        store.revoke(principal_id, actor_did)


async def revoke_for_denial(
    request: Request, row: PendingApproval, actor_did: str
) -> JSONResponse | None:
    """Revoke the pending principal a denied ``principal_issue`` row names."""
    principal_id = row.arguments.get("principal_id", "")
    result = await _in_store(
        request,
        f"approval:{row.id}",
        "approval.deny",
        lambda store: _revoke_requested(store, principal_id, actor_did),
    )
    return result if isinstance(result, JSONResponse) else None


routes = [
    Route(DELEGATIONS_PATH, list_delegations, methods=["GET"]),
    Route(DELEGATIONS_PATH, create_delegation, methods=["POST"]),
    Route(DELEGATIONS_PATH + "/{id}/revoke", revoke_delegation, methods=["POST"]),
    Route(PRINCIPALS_PATH, list_principals, methods=["GET"]),
    Route(PRINCIPALS_PATH, issue_principal, methods=["POST"]),
    Route(PRINCIPALS_PATH + "/{id}/revoke", revoke_principal, methods=["POST"]),
]

__all__ = [
    "DELEGATIONS_PATH",
    "MAX_DELEGATION_TTL_SECONDS",
    "PRINCIPALS_PATH",
    "DelegationCreate",
    "PrincipalIssue",
    "activate_for_approval",
    "revoke_for_denial",
    "routes",
]
