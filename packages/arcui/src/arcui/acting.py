"""Who is acting on a request, and may they decide? (SPEC-087 COMP-006).

:func:`acting_identity` turns the auth layer's ``request.state.principal`` into
one :class:`ActingIdentity` that routes and audit rows share:

=====================  ==============  ==========  ===============  ============
caller                 actor_did       via_app     roles            named_person
=====================  ==============  ==========  ===============  ============
session user           account DID     None        the user's       True
static token           session DID     None        token role       False
app alone              app DID         None        none             False
app for a person       person DID      app DID     the person's     True
=====================  ==============  ==========  ===============  ============

An app adds no role to the person it acts for (REQ-562): the roles are the
person's, and what the app may touch is bounded separately by its scopes.

:func:`decider` adds the decision rules for gates, human_input, task review and
approvals (REQ-563, REQ-571). :func:`is_control_allowed` replaces the bare
operator check on app-reachable routes.

Import direction: this module imports :mod:`arcui.audit` and
:mod:`arcui.principals` at module level; :mod:`arcui.audit` imports this module
lazily inside ``_request_audit_fields`` (``principals`` already imports
``audit``, so the reverse edge must be the lazy one).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from arctrust.principals import DECISION_SCOPES, PrincipalStore, Scope
from arctrust.users import OPERATOR
from starlette.requests import Request

from arcui.audit import emit_mutation_audit, ui_session_actor
from arcui.principals import PrincipalsConfig, RequestPrincipal, scope_allowed

logger = logging.getLogger(__name__)

OPERATOR_ACCOUNT_REQUIRED = "operator_account_required"
DELEGATION_REQUIRED = "delegation_required"
AUTHORITY_UNAVAILABLE = "account_authority_unavailable"
UNNAMED_DECISION_OPERATION = "operator_token_decision"


@dataclass(frozen=True)
class ActingIdentity:
    """The one identity a route records and authorizes against."""

    actor_did: str
    on_behalf_of: str | None
    via_app: str | None
    roles: frozenset[str]
    session_id: str
    named_person: bool


@dataclass(frozen=True)
class Refusal:
    """A refused decision: the stable error code and the HTTP status to send."""

    code: str
    status: int


def acting_identity(request: Request) -> ActingIdentity:
    """The acting identity for an authenticated request (see the module table).

    A request with no principal on its state (a bare test app, or a refusal
    recorded before authentication finished) acts as its session pseudo-DID
    with no roles and no named person.
    """
    principal: RequestPrincipal | None = getattr(request.state, "principal", None)
    session_id = str(getattr(request.state, "session_id", None) or "unknown")
    if principal is None:
        return ActingIdentity(
            ui_session_actor(request), None, None, frozenset(), session_id, False
        )
    if principal.kind != "app":
        named = principal.kind == "user"
        return ActingIdentity(principal.did, None, None, principal.roles, session_id, named)
    if principal.on_behalf_of is None:
        return ActingIdentity(principal.did, None, None, frozenset(), session_id, False)
    person = principal.on_behalf_of
    return ActingIdentity(person, person, principal.did, principal.roles, session_id, True)


def decider(request: Request, scope: Scope) -> ActingIdentity | Refusal:
    """The identity that may record a decision under ``scope``, or a refusal.

    Rules, in order (every refusal writes one audit row):

    1. no principal, or no named person where the tier requires one
       (federal always; enterprise when ``require_named_decider``)
       -> 403 ``operator_account_required``;
    2. at federal, an app deciding for a person needs a live delegation the
       person made for this app and scope -> else 403 ``delegation_required``;
       an unreachable authority -> 503, never allow;
    3. an unnamed decision allowed at enterprise is recorded with one
       ``operator_token_decision`` warning row.

    Synchronous: the delegation read touches the account authority, so async
    callers run this off the event loop.
    """
    identity = acting_identity(request)
    config: PrincipalsConfig = request.app.state.principals_config
    principal = getattr(request.state, "principal", None)
    if principal is None or (not identity.named_person and config.named_decider_required):
        return _refuse(request, scope, Refusal(OPERATOR_ACCOUNT_REQUIRED, 403))
    if config.tier == "federal" and identity.via_app is not None and scope in DECISION_SCOPES:
        refusal = _check_delegation(request, identity, scope)
        if refusal is not None:
            return _refuse(request, scope, refusal)
    if not identity.named_person and config.tier == "enterprise":
        _warn_unnamed_decision(request, scope)
    return identity


def is_control_allowed(request: Request, scope: Scope, agent_did: str | None) -> bool:
    """Operator session / operator token, or an app holding ``scope`` for ``agent_did``.

    An app acting alone is scope-bound only. An app acting for a person also
    needs the person to be able to do it themselves (REQ-562): the person holds
    the operator role. ``gates:resolve`` is the exception: the person's
    approver entries are enforced later by the workflow control plane, so a
    viewer listed as a gate approver may resolve that gate through an app.
    """
    principal: RequestPrincipal | None = getattr(request.state, "principal", None)
    if principal is None:
        return False
    if principal.kind != "app":
        return OPERATOR in principal.roles
    config: PrincipalsConfig = request.app.state.principals_config
    if not scope_allowed(principal, scope, agent_did, config.wildcards_allowed):
        return False
    if principal.on_behalf_of is None or scope == Scope.GATES_RESOLVE:
        return True
    return OPERATOR in principal.roles


def _check_delegation(request: Request, identity: ActingIdentity, scope: Scope) -> Refusal | None:
    """None when the person delegated ``scope`` to this app and it is live now."""
    factory: Callable[[], PrincipalStore] | None = getattr(
        request.app.state, "principal_store_factory", None
    )
    if factory is None or identity.on_behalf_of is None or identity.via_app is None:
        return Refusal(AUTHORITY_UNAVAILABLE, 503)
    try:
        live = factory().live_delegation(
            identity.on_behalf_of, identity.via_app, scope, datetime.now(UTC)
        )
    except Exception as exc:  # authority errors are varied (vault, anchor, I/O); fail closed
        logger.error("acting.delegation_authority_unavailable error=%s", type(exc).__name__)
        return Refusal(AUTHORITY_UNAVAILABLE, 503)
    return None if live is not None else Refusal(DELEGATION_REQUIRED, 403)


def _refuse(request: Request, scope: Scope, refusal: Refusal) -> Refusal:
    path = request.url.path
    emit_mutation_audit(
        request,
        target=path,
        operation=f"{request.method} {path}",
        outcome="error" if refusal.status >= 500 else "denied",
        detail=f"{refusal.code} scope={scope.value}",
    )
    return refusal


def _warn_unnamed_decision(request: Request, scope: Scope) -> None:
    emit_mutation_audit(
        request,
        target=request.url.path,
        operation=UNNAMED_DECISION_OPERATION,
        outcome="applied",
        detail=f"decision without a named person scope={scope.value}",
    )
    # The warning is not the request's outcome: the route still records its own.
    request.state.mutation_audited = False


__all__ = [
    "ActingIdentity",
    "Refusal",
    "acting_identity",
    "decider",
    "is_control_allowed",
]
