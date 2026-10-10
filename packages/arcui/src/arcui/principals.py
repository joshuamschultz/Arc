"""App principals at the dashboard boundary (SPEC-087 COMP-005).

An app principal is a non-human caller (a script, an integration, a SaaS app)
holding an ``arcp_`` bearer secret or an Ed25519 request-signing key minted by
the account authority. This module is the whole principal path of
:class:`arcui.auth.AuthMiddleware`; the middleware only branches here.

Deny by default: an app reaches only the rows of :data:`APP_ROUTES`, never a
hard-denied prefix, and only with a scope bound to the agent it touches. Every
refusal writes one audit row (``emit_mutation_audit``) and answers a fixed
error code, so the wire never says why beyond the code.

Pipeline (each step is one small function below):

1. authority factory present, else 503 ``account_authority_unavailable``;
2. credential verified (bearer, or signed when the tier requires it), else 401
   ``invalid_principal``; an authority failure while loading is 503;
3. per-principal rate limit, else 429;
4. hard-denied prefix or no matching row, else 403 ``route_not_allowed``;
5. a row whose agent comes from the path: scope + agent check, else 403
   ``scope_denied``;
6. ``X-Arc-On-Behalf-Of`` resolved to a live, enabled, allowlisted person,
   else 403 ``on_behalf_of_denied`` (unknown, disabled and not-allowlisted
   people look identical to the app);
7. ``last_used_at`` touched, best effort: a touch failure is logged and the
   request continues (it records use, it grants nothing).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from arctrust.principal_auth import (
    HEADER_ON_BEHALF_OF,
    HEADER_PRINCIPAL,
    REPLAY_WINDOW_SECONDS,
    AuthenticatedPrincipal,
    PrincipalRejected,
    SignedRequest,
    parse_bearer,
    verify_bearer,
    verify_signed,
)
from arctrust.principals import READ_SCOPES, WILDCARD, PrincipalStore, Scope, Tier
from arctrust.replay import ReplayCache
from arctrust.users import OPERATOR
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit

logger = logging.getLogger(__name__)

ENTERPRISE_REQUIRE_NAMED_DECIDER_DEFAULT = False
"""Release default for ``[ui.principals] require_named_decider`` at enterprise.

``False`` this release: an enterprise operator token may still record a decision
(with an ``operator_token_decision`` warning row). The next release flips this to
``True`` per the SPEC-087 README Rollout section; a test pins the value so the
flip is a deliberate one-line change.
"""

BEARER_PREFIX = "arcp_"
_MAX_PERSON_REF_CHARS = 320  # longest valid email address
_WINDOW_SECONDS = 60.0

INVALID_PRINCIPAL = "invalid_principal"
AUTHORITY_UNAVAILABLE = "account_authority_unavailable"
ROUTE_NOT_ALLOWED = "route_not_allowed"
SCOPE_DENIED = "scope_denied"
ON_BEHALF_OF_DENIED = "on_behalf_of_denied"
RATE_LIMITED = "rate_limited"
ON_BEHALF_OF_IGNORED = "principal.on_behalf_of.ignored"


# ---------------------------------------------------------------------------
# Configuration ([ui.principals])
# ---------------------------------------------------------------------------


class PrincipalsConfig(BaseModel):
    """``[ui.principals]`` plus the deployment tier the launcher resolved.

    ``tier`` is never read from the section itself: the launcher stamps the
    deployment's effective tier so a config file cannot loosen it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: Tier
    require_signed_requests: bool = False
    require_named_decider: bool | None = None
    rate_per_minute: int = Field(default=600, gt=0)

    @property
    def signed_required(self) -> bool:
        """Federal always signs; enterprise when configured; personal never."""
        if self.tier == "federal":
            return True
        return self.tier == "enterprise" and self.require_signed_requests

    @property
    def named_decider_required(self) -> bool:
        """Must a decision name a person (no static-token decisions)?"""
        if self.tier == "federal":
            return True
        if self.tier == "personal":
            return False
        if self.require_named_decider is None:
            return ENTERPRISE_REQUIRE_NAMED_DECIDER_DEFAULT
        return self.require_named_decider

    @property
    def wildcards_allowed(self) -> bool:
        """``'*'`` agent and subject bindings are honoured at personal only."""
        return self.tier == "personal"


# ---------------------------------------------------------------------------
# The typed principal every authenticated /api request carries
# ---------------------------------------------------------------------------


PrincipalKind = Literal["user", "app", "operator"]


@dataclass(frozen=True)
class RequestPrincipal:
    """Who is calling: a signed-in person, a static token, or an app.

    Set on ``request.state.principal`` for every authenticated /api request.
    ``scopes`` maps each granted scope to its agent DIDs (apps only); it is
    empty for people and static tokens, whose authority comes from ``roles``.
    """

    did: str
    kind: PrincipalKind
    scopes: Mapping[Scope, frozenset[str]] = field(default_factory=dict)
    on_behalf_of: str | None = None
    roles: frozenset[str] = frozenset()


def session_principal(did: str, roles: tuple[str, ...]) -> RequestPrincipal:
    """A signed-in person; their roles come from the account record."""
    return RequestPrincipal(did=did, kind="user", roles=frozenset(roles))


def static_token_principal(session_did: str, role: str) -> RequestPrincipal:
    """The break-glass static token; names a session, never a person."""
    return RequestPrincipal(did=session_did, kind="operator", roles=frozenset({role}))


def _app_principal(verified: AuthenticatedPrincipal) -> RequestPrincipal:
    scopes = {scope: frozenset(agents) for scope, agents in verified.scopes.items()}
    return RequestPrincipal(did=verified.did, kind="app", scopes=scopes)


# ---------------------------------------------------------------------------
# The app route table (deny by default)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppRoute:
    """One (method, path) an app may reach.

    ``scope=None`` means the route resolves the scope itself and MUST call
    :func:`require_scope`. ``agent_from_path`` returns the agent's roster label
    from the path; the middleware resolves it to a DID and checks the scope.
    """

    method: str
    pattern: re.Pattern[str]
    scope: Scope | None
    agent_from_path: Callable[[re.Match[str]], str] | None = None


def _agent_label(match: re.Match[str]) -> str:
    return match.group("agent")


APP_ROUTES: tuple[AppRoute, ...] = (
    AppRoute("POST", re.compile(r"/api/workflows/[^/]+/run"), Scope.WORKFLOWS_RUN),
    AppRoute("GET", re.compile(r"/api/workflow-runs/[^/]+"), Scope.RUNS_READ),
    AppRoute("GET", re.compile(r"/api/runs/[^/]+/timeline"), Scope.RUNS_READ),
    AppRoute("POST", re.compile(r"/api/workflow-tasks/.+/gate"), None),
    AppRoute("POST", re.compile(r"/api/tasks/.+/approve"), None),
    AppRoute("POST", re.compile(r"/api/tasks/.+/reject"), None),
    AppRoute("POST", re.compile(r"/api/team/tasks"), Scope.TASKS_WRITE),
    AppRoute(
        "POST",
        re.compile(r"/api/agents/(?P<agent>[^/]+)/inbox"),
        Scope.INBOX_SEND,
        agent_from_path=_agent_label,
    ),
)

HARD_DENIED_PREFIXES: tuple[str, ...] = (
    "/api/approvals",
    "/api/standing-grants",
    "/api/principals",
    "/api/users",
    "/api/trust",
    # Delegations are created by the person, in a session, never by an app.
    "/api/me",
)
# Signing routes (``/api/workflows/{id}/request-signature``) end in this segment.
_SIGNING_SEGMENT = re.compile(r"/[^/]*signature(/|$)")


def is_hard_denied(path: str) -> bool:
    """True for paths an app may never reach, whatever it holds."""
    if any(path == prefix or path.startswith(prefix + "/") for prefix in HARD_DENIED_PREFIXES):
        return True
    return _SIGNING_SEGMENT.search(path) is not None


def match_app_route(method: str, path: str) -> AppRoute | None:
    """The row whose method and whole path match, or None (deny)."""
    for route in APP_ROUTES:
        if route.method == method and route.pattern.fullmatch(path) is not None:
            return route
    return None


# ---------------------------------------------------------------------------
# Scope checks
# ---------------------------------------------------------------------------


def require_scope(
    request: Request,
    scope: Scope,
    *,
    agent_did: str | None,
    target: str,
    operation: str,
) -> JSONResponse | None:
    """None when the caller may use ``scope`` on ``agent_did``; else an audited 403.

    Operator sessions and the operator token pass every scope; viewers pass
    only :data:`READ_SCOPES`; an app passes only when it holds ``scope`` bound
    to ``agent_did`` (fail closed when no agent is named).
    """
    principal = getattr(request.state, "principal", None)
    if scope_allowed(principal, scope, agent_did, _config(request).wildcards_allowed):
        return None
    emit_mutation_audit(
        request,
        target=target,
        operation=operation,
        outcome="denied",
        detail=f"{SCOPE_DENIED} scope={scope.value} agent={agent_did}",
    )
    return JSONResponse(
        {"error": SCOPE_DENIED, "scope": scope.value, "agent": agent_did}, status_code=403
    )


def scope_allowed(
    principal: RequestPrincipal | None,
    scope: Scope,
    agent_did: str | None,
    wildcards_allowed: bool,
) -> bool:
    """The pure scope rule behind :func:`require_scope` (no audit, no response)."""
    if principal is None:
        return False
    if principal.kind == "app":
        return _app_holds(principal, scope, agent_did, wildcards_allowed)
    if OPERATOR in principal.roles:
        return True
    return scope in READ_SCOPES


def _app_holds(
    principal: RequestPrincipal, scope: Scope, agent_did: str | None, wildcards_allowed: bool
) -> bool:
    if agent_did is None:
        return False
    agents = principal.scopes.get(scope, frozenset())
    return agent_did in agents or (wildcards_allowed and WILDCARD in agents)


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class PrincipalRateLimiter:
    """Fixed one-minute window per principal DID.

    The window opens on a principal's first call and resets a full minute
    later; refused calls do not extend it. Keys are verified principal DIDs
    only (the limiter runs after authentication), so the map is bounded by the
    number of live principals. Used from the event loop only; no locking.
    """

    def __init__(self, rate_per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self._rate = rate_per_minute
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, principal_did: str) -> bool:
        """Spend one call from ``principal_did``'s budget; False when it is spent."""
        now = self._clock()
        started, used = self._windows.get(principal_did, (now, 0))
        if now - started >= _WINDOW_SECONDS:
            started, used = now, 0
        if used >= self._rate:
            return False
        self._windows[principal_did] = (started, used + 1)
        return True


def new_replay_cache() -> ReplayCache:
    """The signed-request nonce cache, sized to the envelope's replay window."""
    return ReplayCache(window_seconds=REPLAY_WINDOW_SECONDS)


# ---------------------------------------------------------------------------
# The principal path of AuthMiddleware
# ---------------------------------------------------------------------------


def is_principal_request(request: Request, bearer: str) -> bool:
    """Does this request present an app credential (checked before static tokens)?"""
    return bearer.startswith(BEARER_PREFIX) or HEADER_PRINCIPAL in request.headers


async def authenticate_principal(request: Request, bearer: str) -> RequestPrincipal | JSONResponse:
    """Run the whole principal pipeline; the principal, or the refusal to send."""
    request.state.on_behalf_of = None
    factory = getattr(request.app.state, "principal_store_factory", None)
    if factory is None:
        return _authority_unavailable(request, "no principal authority configured")
    verified = await _verify_credential(request, bearer, factory)
    if isinstance(verified, JSONResponse):
        return verified
    store, authenticated = verified
    principal = _bind_principal(request, authenticated)
    refused = _check_rate(request, principal) or _check_route(request)
    if refused is not None:
        return refused
    refused = await _check_path_agent(request)
    if refused is not None:
        return refused
    person = await _resolve_on_behalf_of(request, authenticated)
    if isinstance(person, JSONResponse):
        return person
    if person is not None:
        # The person's own roles ride along; the app adds none (REQ-562).
        principal = replace(principal, on_behalf_of=person.did, roles=person.roles)
    request.state.principal = principal
    request.state.on_behalf_of = principal.on_behalf_of
    await _touch(store, authenticated)
    return principal


def _config(request: Request) -> PrincipalsConfig:
    config: PrincipalsConfig = request.app.state.principals_config
    return config


def _bind_principal(request: Request, verified: AuthenticatedPrincipal) -> RequestPrincipal:
    """Attribute everything after this point (including refusals) to the app."""
    principal = _app_principal(verified)
    request.state.principal = principal
    request.state.role = "app"
    request.state.account_did = None
    return principal


# --- step 2: credential -------------------------------------------------------


async def _verify_credential(
    request: Request, bearer: str, factory: Callable[[], PrincipalStore]
) -> tuple[PrincipalStore, AuthenticatedPrincipal] | JSONResponse:
    signed = HEADER_PRINCIPAL in request.headers
    if _config(request).signed_required and not signed:
        return _invalid_principal(request, "signed_required")
    try:
        if signed:
            return await _verify_signed(request, factory)
        return await _verify_bearer(bearer, factory)
    except PrincipalRejected as exc:
        return _invalid_principal(request, exc.reason)
    except _AuthorityError:
        return _authority_unavailable(request, "principal authority failed")


class _AuthorityError(Exception):
    """The principal authority could not be opened or read."""


async def _load(
    factory: Callable[[], PrincipalStore],
    read: Callable[[PrincipalStore], Any],
) -> tuple[PrincipalStore, Any]:
    """Open the store and read one record off the event loop; any failure is 503."""

    def _open_and_read() -> tuple[PrincipalStore, Any]:
        store = factory()
        return store, read(store)

    try:
        return await asyncio.to_thread(_open_and_read)
    except Exception as exc:  # authority errors are varied (vault, anchor, I/O)
        logger.error("principal.authority_unavailable error=%s", type(exc).__name__)
        raise _AuthorityError from exc


async def _verify_bearer(
    bearer: str, factory: Callable[[], PrincipalStore]
) -> tuple[PrincipalStore, AuthenticatedPrincipal]:
    principal_id, secret = parse_bearer(bearer)
    store, record = await _load(factory, lambda s: s.get(principal_id))
    return store, verify_bearer(record, secret, now=datetime.now(UTC))


async def _verify_signed(
    request: Request, factory: Callable[[], PrincipalStore]
) -> tuple[PrincipalStore, AuthenticatedPrincipal]:
    # Starlette caches the body, so the route still reads it after this.
    body = await request.body()
    envelope = SignedRequest.from_headers(
        request.headers, request.method, request.url.path, request.url.query, body
    )
    store, record = await _load(factory, lambda s: s.get_by_did(envelope.principal_did))
    cache: ReplayCache = request.app.state.principal_replay_cache
    return store, verify_signed(record, envelope, cache, now=datetime.now(UTC))


# --- steps 3-5: rate, route, agent --------------------------------------------


def _check_rate(request: Request, principal: RequestPrincipal) -> JSONResponse | None:
    limiter: PrincipalRateLimiter = request.app.state.principal_rate_limiter
    if limiter.allow(principal.did):
        return None
    return _refuse(request, 429, RATE_LIMITED, "rate limit exceeded")


def _check_route(request: Request) -> JSONResponse | None:
    path = request.url.path
    if is_hard_denied(path) or match_app_route(request.method, path) is None:
        return _refuse(request, 403, ROUTE_NOT_ALLOWED, "route not in the app table")
    return None


async def _check_path_agent(request: Request) -> JSONResponse | None:
    """Scope + agent check for rows whose agent is named in the path."""
    path = request.url.path
    route = match_app_route(request.method, path)
    if route is None or route.scope is None or route.agent_from_path is None:
        return None
    match = route.pattern.fullmatch(path)
    if match is None:
        return None
    agent_did = await roster_did(request, route.agent_from_path(match))
    return require_scope(
        request,
        route.scope,
        agent_did=agent_did,
        target=path,
        operation=f"{request.method} {path}",
    )


async def roster_did(request: Request, label: str) -> str | None:
    """The DID of the roster agent labelled ``label``; None when unknown."""
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    rows = await asyncio.to_thread(provider)
    for row in rows:
        if row.agent_id == label:
            return str(row.did)
    return None


# --- step 6: on behalf of -----------------------------------------------------


@dataclass(frozen=True)
class _Person:
    did: str
    roles: frozenset[str]


async def _resolve_on_behalf_of(
    request: Request, verified: AuthenticatedPrincipal
) -> _Person | None | JSONResponse:
    """The person (DID + roles), None when no header, or an audited refusal."""
    ref = request.headers.get(HEADER_ON_BEHALF_OF)
    if ref is None:
        return None
    if not ref or len(ref) > _MAX_PERSON_REF_CHARS:
        return _refuse_person(request, "malformed")
    try:
        person = await _lookup_person(request, ref)
    except _AuthorityError:
        return _authority_unavailable(request, "account authority failed")
    if person is None or person.disabled:
        return _refuse_person(request, "unknown_or_disabled")
    if not _subject_allowed(verified.subjects, person.did, _config(request)):
        return _refuse_person(request, "not_allowlisted")
    return _Person(did=str(person.did), roles=frozenset(person.roles))


async def _lookup_person(request: Request, ref: str) -> Any:
    """The account named by DID or email, or None; authority failure raises."""
    factory = getattr(request.app.state, "user_store_factory", None)
    if factory is None:
        raise _AuthorityError

    def _find() -> Any:
        users = factory()
        if ref.startswith("did:"):
            return next((user for user in users.list() if user.did == ref), None)
        return users.get(ref)

    try:
        return await asyncio.to_thread(_find)
    except Exception as exc:  # authority errors are varied (vault, anchor, I/O)
        logger.error("principal.account_authority_unavailable error=%s", type(exc).__name__)
        raise _AuthorityError from exc


def _subject_allowed(subjects: tuple[str, ...], person_did: str, config: PrincipalsConfig) -> bool:
    return person_did in subjects or (config.wildcards_allowed and WILDCARD in subjects)


def _refuse_person(request: Request, reason: str) -> JSONResponse:
    # One body for every reason: the app cannot learn which people exist.
    return _refuse(request, 403, ON_BEHALF_OF_DENIED, reason)


# --- step 7: touch ------------------------------------------------------------


async def _touch(store: PrincipalStore, verified: AuthenticatedPrincipal) -> None:
    """Record use; never fails the request (it records, it grants nothing)."""
    try:
        await asyncio.to_thread(store.touch, verified.principal_id, datetime.now(UTC))
    except Exception as exc:  # a lost last_used_at must not deny a verified call
        logger.warning(
            "principal.touch_failed principal=%s error=%s", verified.did, type(exc).__name__
        )


# --- refusals -----------------------------------------------------------------


def _refuse(request: Request, status: int, code: str, detail: str) -> JSONResponse:
    path = request.url.path
    emit_mutation_audit(
        request,
        target=path,
        operation=f"{request.method} {path}",
        outcome="denied",
        detail=f"{code}: {detail}",
    )
    return JSONResponse({"error": code}, status_code=status)


def _invalid_principal(request: Request, reason: str) -> JSONResponse:
    return _refuse(request, 401, INVALID_PRINCIPAL, reason)


def _authority_unavailable(request: Request, detail: str) -> JSONResponse:
    path = request.url.path
    emit_mutation_audit(
        request,
        target=path,
        operation=f"{request.method} {path}",
        outcome="error",
        detail=f"{AUTHORITY_UNAVAILABLE}: {detail}",
    )
    return JSONResponse({"error": AUTHORITY_UNAVAILABLE}, status_code=503)


def record_ignored_on_behalf_of(request: Request) -> None:
    """A person or static token sent ``X-Arc-On-Behalf-Of``: serve, ignore, audit.

    Only apps act for someone. The anomaly row is not the request's outcome, so
    the route's own refusal (if any) is still recorded by the auth layer.
    """
    if HEADER_ON_BEHALF_OF not in request.headers:
        return
    emit_mutation_audit(
        request,
        target=request.url.path,
        operation=ON_BEHALF_OF_IGNORED,
        outcome="denied",
        detail="on-behalf-of header ignored for a non-app caller",
    )
    request.state.mutation_audited = False


__all__ = [
    "APP_ROUTES",
    "ENTERPRISE_REQUIRE_NAMED_DECIDER_DEFAULT",
    "HARD_DENIED_PREFIXES",
    "AppRoute",
    "PrincipalRateLimiter",
    "PrincipalsConfig",
    "RequestPrincipal",
    "authenticate_principal",
    "is_hard_denied",
    "is_principal_request",
    "match_app_route",
    "new_replay_cache",
    "record_ignored_on_behalf_of",
    "require_scope",
    "roster_did",
    "scope_allowed",
    "session_principal",
    "static_token_principal",
]
