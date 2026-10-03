"""People from the browser (J-O1) — first operator, invites, roles, disable, reset.

A customer finishes every account step in arcui. Nothing here ever names a CLI
command, and nothing here re-implements account rules: every write goes through
the same :class:`arctrust.UserStore` ``arc user`` uses (Argon2id hashing, the
12-character floor, unique handles, minted user DIDs).

Three trust rules:

* **First operator.** Only while no operator account exists, and only with the
  one-time setup code this process wrote to its own log. The code is minted the
  first time the sign-in screen asks (``GET /api/auth/mode``), works once, and
  guessing it locks the form for five minutes. There is no open self-signup.
* **Everything after** is operator-only, and each attempt — applied or denied —
  is one ``ui.mutation`` audit record naming the actor.
* **Links work once.** Invite and reset links are random tokens held only as
  hashes in this process's memory, expire after 72 hours, and are consumed by the
  first successful use. A restart voids every outstanding link; the invitee asks
  for a new one. Keeping them in memory means no file an attacker could edit to
  mint an operator invite. The role travels with the server-side record, never
  with the accepting request.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar
from urllib.parse import unquote

from arctrust.users import OPERATOR, User, UserStore, UserStoreError
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.auth import ui_session_actor

logger = logging.getLogger(__name__)

_LINK_TTL_SECONDS = 72 * 60 * 60
_SETUP_THROTTLE_KEY = "first-run-setup:"
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread from a log
_CODE_GROUPS = 6
_CODE_GROUP_LEN = 4

Role = Literal["viewer", "operator"]
_T = TypeVar("_T")


# --- request bodies ---------------------------------------------------------


class _Strict(BaseModel):
    """Unknown fields are refused, so a body cannot smuggle a role or a flag."""

    model_config = ConfigDict(extra="forbid")


class SetupBody(_Strict):
    setup_code: str
    email: str
    password: str
    display_name: str = ""


class AddUserBody(_Strict):
    email: str
    password: str
    role: Role
    display_name: str = ""


class InviteBody(_Strict):
    email: str
    role: Role


class RoleBody(_Strict):
    role: Role


class LinkCheckBody(_Strict):
    token: str


class LinkAcceptBody(_Strict):
    token: str
    password: str
    display_name: str = ""


# --- process-local state ----------------------------------------------------


@dataclass(frozen=True)
class _Link:
    email: str
    kind: Literal["invite", "reset"]
    role: Role
    expires_at: float


@dataclass
class _PeopleState:
    """Setup code and one-time links. Process memory only, by design (see module doc)."""

    setup_code: str | None = None
    setup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    links: dict[str, _Link] = field(default_factory=dict)
    links_lock: threading.Lock = field(default_factory=threading.Lock)


_STATE_GUARD = threading.Lock()


def _state(request: Request) -> _PeopleState:
    """This app's people state, created on first use (owned by this module)."""
    with _STATE_GUARD:
        state: _PeopleState | None = getattr(request.app.state, "people", None)
        if state is None:
            state = _PeopleState()
            request.app.state.people = state
        return state


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_link(
    request: Request, *, email: str, kind: Literal["invite", "reset"], role: Role
) -> tuple[str, float]:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + _LINK_TTL_SECONDS
    state = _state(request)
    with state.links_lock:
        now = time.time()
        for key in [k for k, v in state.links.items() if v.expires_at <= now]:
            del state.links[key]
        state.links[_digest(token)] = _Link(
            email=email, kind=kind, role=role, expires_at=expires_at
        )
    return token, expires_at


def _peek_link(request: Request, token: str) -> _Link | None:
    state = _state(request)
    with state.links_lock:
        link = state.links.get(_digest(token))
    if link is None or link.expires_at <= time.time():
        return None
    return link


def _consume_link(request: Request, token: str) -> _Link | None:
    """Remove and return the link — exactly one caller ever gets it."""
    state = _state(request)
    with state.links_lock:
        link = state.links.pop(_digest(token), None)
    if link is None or link.expires_at <= time.time():
        return None
    return link


def _restore_link(request: Request, token: str, link: _Link) -> None:
    """Put back a link whose use failed for a reason the person can fix (weak password)."""
    state = _state(request)
    with state.links_lock:
        state.links.setdefault(_digest(token), link)


def _mint_setup_code() -> str:
    groups = [
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP_LEN))
        for _ in range(_CODE_GROUPS)
    ]
    return "-".join(groups)


def _normalize_code(code: str) -> str:
    return code.strip().upper().replace(" ", "")


# --- shared helpers ---------------------------------------------------------


def user_store(request: Request) -> UserStore:
    """The deployment's account authority, or raise when none is configured."""
    factory = getattr(request.app.state, "user_store_factory", None)
    if factory is None:
        raise RuntimeError("account authority is unavailable")
    store: UserStore = factory()
    return store


def issue_session(request: Request, user: User) -> dict[str, Any]:
    """Sign ``user`` in and return the body every sign-in route answers with."""
    role = "operator" if user.is_operator else "viewer"
    session = request.app.state.auth_config.sessions.issue(
        email=user.email, did=user.did, role=role
    )
    return {
        "token": session.token,
        "email": user.email,
        "did": user.did,
        "role": role,
        "expires_at": session.expires_at,
    }


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request, model: type[_Strict]) -> Any:
    try:
        raw = await request.json()
    except Exception:  # reason: a malformed body is a bad request, not a 500
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


def _has_operator(store: UserStore) -> bool:
    # Any operator at all, even a disabled one: first-run setup is for an empty
    # deployment, never a way back in after the operators were locked out.
    return any(u.is_operator for u in store.list())


def _audit(request: Request, target: str, operation: str, outcome: str, detail: str = "") -> None:
    actor = ui_session_actor(request) if getattr(request.state, "role", None) else "anonymous"
    emit_mutation_audit(
        request,
        target=target,
        operation=operation,
        outcome=outcome,
        detail=f"actor={actor} {detail}".strip(),
    )


def _require_operator(request: Request, target: str, operation: str) -> JSONResponse | None:
    if getattr(request.state, "role", None) == "operator":
        return None
    _audit(request, target, operation, "denied", "not an operator")
    return _error("Only an operator can manage people.", 403)


async def _in_store(request: Request, fn: Callable[[UserStore], _T]) -> _T:
    return await asyncio.to_thread(lambda: fn(user_store(request)))


# --- first run --------------------------------------------------------------


async def first_run_available(request: Request) -> bool:
    """True while no operator exists. Mints and logs the setup code on first ask."""
    has_operator = await _in_store(request, _has_operator)
    if has_operator:
        return False
    state = _state(request)
    async with state.setup_lock:
        if state.setup_code is None:
            state.setup_code = _mint_setup_code()
            # The one place the code is shown. Whoever can read this log already
            # controls the box; whoever cannot has no way to claim the account.
            logger.warning(
                "ARC FIRST-RUN SETUP CODE: %s (works once, only until the first operator "
                "account exists; type it on the sign-in page)",
                state.setup_code,
            )
    return True


async def setup(request: Request) -> JSONResponse:
    """POST /api/auth/setup — create the first operator with the logged setup code."""
    target = "user:first-operator"
    body = await _body(request, SetupBody)
    if body is None:
        return _error("Enter the setup code, your email, and a password.", 400)
    sessions = request.app.state.auth_config.sessions
    state = _state(request)
    async with state.setup_lock:
        try:
            if await _in_store(request, _has_operator):
                _audit(request, target, "user.setup", "denied", "an operator already exists")
                return _error("This Arc already has an operator. Sign in instead.", 409)
        except Exception as exc:  # reason: absent authority must not 500 the sign-in page
            logger.error("users.setup store_unreadable class=%s", type(exc).__name__)
            return _error("Account authority is unavailable. Try again shortly.", 503)
        if sessions.locked_out(_SETUP_THROTTLE_KEY):
            _audit(request, target, "user.setup", "denied", "locked out")
            return _error("Too many wrong setup codes. Try again in five minutes.", 429)
        expected = state.setup_code
        if expected is None or not hmac.compare_digest(
            _normalize_code(body.setup_code).encode(), expected.replace(" ", "").encode()
        ):
            sessions.record_failure(_SETUP_THROTTLE_KEY)
            _audit(request, target, "user.setup", "denied", "wrong setup code")
            return _error("That setup code is not right. Check the server log and try again.", 401)
        try:
            user = await _in_store(
                request,
                lambda store: store.add(
                    body.email, body.password, display_name=body.display_name, roles=(OPERATOR,)
                ),
            )
        except ValueError as exc:
            return _error(_plain(exc), 400)
        except (UserStoreError, RuntimeError) as exc:
            logger.error("users.setup store_failed class=%s", type(exc).__name__)
            return _error("Account authority is unavailable. Try again shortly.", 503)
        # Burned on success: a replay meets "already has an operator" above.
        state.setup_code = None
        sessions.clear_failures(_SETUP_THROTTLE_KEY)
    _audit(request, f"user:{user.email}", "user.setup", "applied", "first operator")
    logger.info("users.first_operator email=%s", user.email)
    return JSONResponse(issue_session(request, user))


def _plain(exc: ValueError) -> str:
    """A store refusal in the person's words (they are already plain)."""
    message = str(exc)
    return message[:1].upper() + message[1:] + ("" if message.endswith(".") else ".")


# --- invite / reset links (unauthenticated: the token is the credential) ----


async def check_link(request: Request) -> JSONResponse:
    """POST /api/auth/invite/check — what a one-time link is for, without using it."""
    body = await _body(request, LinkCheckBody)
    link = _peek_link(request, body.token) if body is not None else None
    if link is None:
        return _error(
            "This link has expired or was already used. Ask an operator for a new one.", 404
        )
    return JSONResponse({"email": link.email, "kind": link.kind, "role": link.role})


async def accept_link(request: Request) -> JSONResponse:
    """POST /api/auth/invite/accept — set a password through a one-time link and sign in."""
    body = await _body(request, LinkAcceptBody)
    if body is None:
        return _error("Enter a password to continue.", 400)
    link = _consume_link(request, body.token)
    if link is None:
        _audit(request, "user:link", "user.link_accept", "denied", "expired or used link")
        return _error(
            "This link has expired or was already used. Ask an operator for a new one.", 404
        )
    try:
        user = await _in_store(request, lambda store: _redeem(store, link, body))
    except ValueError as exc:
        _restore_link(request, body.token, link)
        return _error(_plain(exc), 400)
    except LookupError:
        _audit(request, f"user:{link.email}", "user.link_accept", "denied", "account gone")
        return _error(
            "This link has expired or was already used. Ask an operator for a new one.", 404
        )
    except (UserStoreError, RuntimeError) as exc:
        _restore_link(request, body.token, link)
        logger.error("users.accept store_failed class=%s", type(exc).__name__)
        return _error("Account authority is unavailable. Try again shortly.", 503)
    if link.kind == "reset":
        request.app.state.auth_config.sessions.revoke_user(user.email)
    _audit(
        request, f"user:{user.email}", f"user.{link.kind}_accept", "applied", f"role={link.role}"
    )
    return JSONResponse(issue_session(request, user))


def _redeem(store: UserStore, link: _Link, body: LinkAcceptBody) -> User:
    if link.kind == "invite":
        return store.add(
            link.email, body.password, display_name=body.display_name, roles=(link.role,)
        )
    existing = store.get(link.email)
    if existing is None or existing.disabled:
        raise LookupError(link.email)
    return store.set_password(link.email, body.password)


# --- operator management ----------------------------------------------------


def _row(user: User) -> dict[str, object]:
    return user.redacted()


def _email_param(request: Request) -> str:
    return unquote(request.path_params["email"]).strip().lower()


async def list_users(request: Request) -> JSONResponse:
    """GET /api/users — everyone who can sign in (operator only)."""
    if getattr(request.state, "role", None) != "operator":
        return _error("Only an operator can manage people.", 403)
    users = await _in_store(request, lambda store: store.list())
    return JSONResponse({"users": [_row(u) for u in users]})


async def add_user(request: Request) -> JSONResponse:
    """POST /api/users — add a person with a password the operator sets."""
    refused = _require_operator(request, "user:new", "user.add")
    if refused is not None:
        return refused
    body = await _body(request, AddUserBody)
    if body is None:
        return _error("Enter an email, a role, and a password.", 400)
    try:
        user = await _in_store(
            request,
            lambda store: store.add(
                body.email, body.password, display_name=body.display_name, roles=(body.role,)
            ),
        )
    except ValueError as exc:
        _audit(request, f"user:{body.email}", "user.add", "denied", str(exc))
        return _error(_plain(exc), 400)
    _audit(request, f"user:{user.email}", "user.add", "applied", f"role={body.role}")
    return JSONResponse({"user": _row(user)}, status_code=201)


async def invite_user(request: Request) -> JSONResponse:
    """POST /api/users/invites — a one-time link the invitee uses to set a password."""
    refused = _require_operator(request, "user:invite", "user.invite")
    if refused is not None:
        return refused
    body = await _body(request, InviteBody)
    if body is None or "@" not in body.email:
        return _error("Enter an email and a role.", 400)
    email = body.email.strip().lower()
    if await _in_store(request, lambda store: store.get(email)) is not None:
        return _error(f"{email} already has an account.", 409)
    token, expires_at = _issue_link(request, email=email, kind="invite", role=body.role)
    _audit(request, f"user:{email}", "user.invite", "applied", f"role={body.role}")
    return JSONResponse(
        {
            "link_path": f"/#invite={token}",
            "expires_at": expires_at,
            "email": email,
            "role": body.role,
        },
        status_code=201,
    )


async def reset_link(request: Request) -> JSONResponse:
    """POST /api/users/{email}/reset-link — a one-time link to set a new password."""
    email = _email_param(request)
    refused = _require_operator(request, f"user:{email}", "user.reset")
    if refused is not None:
        return refused
    user = await _in_store(request, lambda store: store.get(email))
    if user is None:
        return _error(f"No account for {email}.", 404)
    role: Role = "operator" if user.is_operator else "viewer"
    token, expires_at = _issue_link(request, email=email, kind="reset", role=role)
    _audit(request, f"user:{email}", "user.reset", "applied", "reset link issued")
    return JSONResponse(
        {"link_path": f"/#invite={token}", "expires_at": expires_at, "email": email, "role": role},
        status_code=201,
    )


def _removes_last_operator(
    store: UserStore, email: str, *, role: str | None, disabled: bool | None
) -> bool:
    """Would this change take the deployment from some active operator to none?"""

    def active_operators(after_change: bool) -> int:
        count = 0
        for user in store.list():
            changed = after_change and user.email == email
            is_operator = (role == OPERATOR) if changed and role is not None else user.is_operator
            is_disabled = disabled if changed and disabled is not None else user.disabled
            count += int(is_operator and not is_disabled)
        return count

    return active_operators(False) > 0 and active_operators(True) == 0


async def set_role(request: Request) -> JSONResponse:
    """PUT /api/users/{email}/role — make someone a viewer or an operator."""
    email = _email_param(request)
    refused = _require_operator(request, f"user:{email}", "user.role")
    if refused is not None:
        return refused
    body = await _body(request, RoleBody)
    if body is None:
        return _error("The role must be viewer or operator.", 400)
    return await _change(request, email, "user.role", role=body.role, disabled=None)


async def disable_user(request: Request) -> JSONResponse:
    """POST /api/users/{email}/disable — block sign-in and end their sessions."""
    email = _email_param(request)
    refused = _require_operator(request, f"user:{email}", "user.disable")
    if refused is not None:
        return refused
    return await _change(request, email, "user.disable", role=None, disabled=True)


async def enable_user(request: Request) -> JSONResponse:
    """POST /api/users/{email}/enable — let a blocked person sign in again."""
    email = _email_param(request)
    refused = _require_operator(request, f"user:{email}", "user.enable")
    if refused is not None:
        return refused
    return await _change(request, email, "user.enable", role=None, disabled=False)


async def _change(
    request: Request, email: str, operation: str, *, role: str | None, disabled: bool | None
) -> JSONResponse:
    def apply(store: UserStore) -> User | str:
        if store.get(email) is None:
            return "missing"
        if _removes_last_operator(store, email, role=role, disabled=disabled):
            return "last-operator"
        if role is not None:
            return store.set_roles(email, (role,))
        return store.set_disabled(email, bool(disabled))

    result = await _in_store(request, apply)
    if result == "missing":
        return _error(f"No account for {email}.", 404)
    if not isinstance(result, User):
        _audit(request, f"user:{email}", operation, "denied", "would leave no operator")
        return _error(
            "Arc needs at least one active operator. Make someone else an operator first.", 409
        )
    if disabled or role is not None:
        # A demoted or blocked person must not keep acting on an old session.
        request.app.state.auth_config.sessions.revoke_user(email)
    _audit(request, f"user:{email}", operation, "applied", f"role={role}" if role else "")
    return JSONResponse({"user": _row(result)})


ROUTES: list[tuple[str, Any, list[str]]] = [
    ("/api/auth/setup", setup, ["POST"]),
    ("/api/auth/invite/check", check_link, ["POST"]),
    ("/api/auth/invite/accept", accept_link, ["POST"]),
    ("/api/users", list_users, ["GET"]),
    ("/api/users", add_user, ["POST"]),
    ("/api/users/invites", invite_user, ["POST"]),
    ("/api/users/{email}/role", set_role, ["PUT"]),
    ("/api/users/{email}/disable", disable_user, ["POST"]),
    ("/api/users/{email}/enable", enable_user, ["POST"]),
    ("/api/users/{email}/reset-link", reset_link, ["POST"]),
]

__all__ = [
    "ROUTES",
    "first_run_available",
    "issue_session",
    "user_store",
]
