"""Sign-in endpoints (SPEC-057 REQ-043).

The token-in-URL bootstrap proves possession of a secret. It cannot say who is
holding it, so an approval recorded against it answers "what happened" but never
"who allowed this" — which is the question that actually gets asked afterwards.
These routes let a person sign in as themselves, and the session they get back
carries their DID into every mutation they make.

Account authority is injected by the deployment. If custody or integrity is
unavailable, these routes fail closed while unrelated UI capabilities may run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes import users as users_routes

logger = logging.getLogger(__name__)


def _store(request: Request) -> Any:
    return users_routes.user_store(request)


def _auth(request: Request) -> Any:
    return request.app.state.auth_config


async def login(request: Request) -> JSONResponse:
    """POST /api/auth/login — email + password in, a session token out."""
    try:
        body = await request.json()
    except Exception:  # reason: a malformed body is a bad request, not a 500
        return JSONResponse({"error": "Expected a JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected a JSON object"}, status_code=400)

    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    if not (email and password):
        return JSONResponse({"error": "Email and password are required"}, status_code=400)

    auth = _auth(request)
    sessions = auth.sessions

    if sessions.locked_out(email):
        # Deliberately explicit. A vague failure here sends people to support
        # convinced their password is broken.
        logger.warning("auth.locked_out email=%s", email)
        return JSONResponse(
            {"error": "Too many failed attempts. Try again in five minutes."},
            status_code=429,
        )

    try:
        user = await asyncio.to_thread(lambda: _store(request).verify(email, password))
    except Exception as exc:  # reason: an unreadable store must not 500 the login page
        logger.error("auth.store_unreadable class=%s", type(exc).__name__)
        return JSONResponse(
            {"error": "Account authority is unavailable. Try again shortly."},
            status_code=503,
        )

    if user is None:
        sessions.record_failure(email)
        logger.warning("auth.failed email=%s", email)
        # One message for wrong password, unknown account, and disabled account:
        # anything more precise is an account-enumeration oracle.
        return JSONResponse({"error": "Email or password is wrong"}, status_code=401)

    sessions.clear_failures(email)
    body = users_routes.issue_session(request, user)
    logger.info("auth.login email=%s role=%s", user.email, body["role"])
    return JSONResponse(body)


async def logout(request: Request) -> JSONResponse:
    """POST /api/auth/logout — drop the caller's session."""
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token:
        _auth(request).sessions.revoke(token)
    return JSONResponse({"ok": True})


async def me(request: Request) -> JSONResponse:
    """GET /api/auth/me — who the caller is, if they are anyone."""
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    session = _auth(request).identify(token)
    if session is None:
        # A static token authenticated successfully but names no person. Say so
        # plainly rather than inventing a user, so the UI can nudge them to sign in.
        return JSONResponse(
            {
                "authenticated": True,
                "anonymous": True,
                "role": getattr(request.state, "role", None),
                "email": None,
                "did": None,
            }
        )
    body = {
        "authenticated": True,
        "anonymous": False,
        "role": session.role,
        "email": session.email,
        "did": session.did,
        "expires_at": session.expires_at,
    }
    # The settings the person can actually change. Read from the store rather
    # than the session so an edit shows up without signing out and back in.
    try:
        user = await asyncio.to_thread(lambda: _store(request).get(session.email))
    except Exception:  # reason: an unreadable store must not break /me
        user = None
    if user is not None:
        body["handle"] = user.handle
        body["display_name"] = user.display_name
        body["pairings"] = dict(user.pairings)
    return JSONResponse(body)


async def update_me(request: Request) -> JSONResponse:
    """PATCH /api/auth/me — change your own name, mention handle, or chat links.

    Scoped to the caller on purpose: a viewer editing their own display name is
    routine, and letting them reach anyone else's record through the same route
    would make a read-only role into a user-admin one. Managing *other* people
    is the operator-only ``/api/users`` surface (Settings -> People).
    """
    session = _auth(request).identify(
        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if session is None:
        # A static token is not a person, so it has no profile to edit.
        return JSONResponse({"error": "Sign in to change your settings"}, status_code=403)

    try:
        body = await request.json()
    except Exception:  # reason: a malformed body is a bad request, not a 500
        return JSONResponse({"error": "Expected a JSON body"}, status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("pairings", {}), dict):
        return JSONResponse({"error": "Expected a JSON object"}, status_code=400)

    try:
        changed, user = await asyncio.to_thread(_update_profile, request, session.email, body)
    except ValueError as exc:
        # Carries the real reason: a taken handle names who holds it.
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("auth.update_me failed class=%s", type(exc).__name__)
        return JSONResponse({"error": "Could not save your settings"}, status_code=503)

    if not changed:
        return JSONResponse({"error": "Nothing to change"}, status_code=400)

    logger.info("auth.settings_changed email=%s fields=%s", session.email, ",".join(changed))
    return JSONResponse({"ok": True, "changed": changed, "user": user.redacted()})


def _update_profile(request: Request, email: str, body: dict[str, Any]) -> tuple[list[str], Any]:
    store = _store(request)
    changed: list[str] = []
    if "display_name" in body:
        changed.append("display_name")
    if "handle" in body:
        changed.append("handle")
    pairings = {
        str(platform): str(external_id).strip() or None
        for platform, external_id in body.get("pairings", {}).items()
    }
    for platform in pairings:
        changed.append(f"pairings.{platform}")
    if not changed:
        return changed, None
    user = store.update_profile(
        email,
        display_name=str(body["display_name"]) if "display_name" in body else None,
        handle=str(body["handle"]) if "handle" in body else None,
        pairings=pairings,
    )
    return changed, user


async def mode(request: Request) -> JSONResponse:
    """GET /api/auth/mode — does this deployment have accounts, and is setup open?

    Unauthenticated on purpose. A fresh install must offer "create the first
    operator account" instead of a sign-in form nobody can satisfy. It leaks only
    whether any account / any operator exists, never which. While no operator
    exists, asking mints the one-time setup code into the server log.
    """
    try:
        has_users = not await asyncio.to_thread(lambda: _store(request).is_empty())
        setup_available = await users_routes.first_run_available(request)
    except Exception:  # reason: absent authority is distinct from an empty store
        return JSONResponse({"error": "Account authority is unavailable"}, status_code=503)
    return JSONResponse({"login_available": has_users, "setup_available": setup_available})


ROUTES = [
    ("/api/auth/login", login, ["POST"]),
    ("/api/auth/logout", logout, ["POST"]),
    ("/api/auth/me", me, ["GET"]),
    ("/api/auth/me", update_me, ["PATCH"]),
    ("/api/auth/mode", mode, ["GET"]),
    # People management rides the auth route table: one mount point for every
    # account route, first-run setup and one-time links included.
    *users_routes.ROUTES,
]

__all__ = ["ROUTES", "login", "logout", "me", "mode", "update_me"]
