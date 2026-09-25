"""Hosted grant delivery and first account claim on separate channels."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)
_MAX_BODY = 8192


class HostedBusyError(RuntimeError):
    """All hosted authority workers are still occupied."""


class ClaimedAccount(Protocol):
    """Account identity returned by a verified first claim."""

    @property
    def email(self) -> str: ...


class HostedClaimService(Protocol):
    """Narrow first-claim capability supplied by hosted composition."""

    def status(self) -> str: ...

    def signed_challenge(self) -> dict[str, Any]: ...

    def install_grant(self, envelope: dict[str, Any]) -> None: ...

    def claim(self, secret: str, password: str) -> ClaimedAccount: ...


class HostedRekeyService(Protocol):
    """New machine key held until an independently checked cloud endorsement."""

    def signed_intent(self) -> dict[str, Any]: ...

    def install_rekey(self, envelope: dict[str, Any]) -> None: ...


def _service(request: Request) -> HostedClaimService | None:
    if not getattr(request.app.state, "hosted", False):
        return None
    return getattr(request.app.state, "hosted_claim", None)


def _rekey_service(request: Request) -> HostedRekeyService | None:
    if not getattr(request.app.state, "hosted", False):
        return None
    return getattr(request.app.state, "hosted_rekey", None)


async def _bounded_call(
    request: Request, operation: Callable[..., Any], *args: Any, timeout: float = 5
) -> Any:
    """Hold one slot until the underlying worker ends, even on disconnect."""
    semaphore = request.app.state.hosted_claim_semaphore
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    except TimeoutError as exc:
        raise HostedBusyError("hosted authority is busy") from exc
    loop = asyncio.get_running_loop()
    try:
        future = request.app.state.hosted_claim_executor.submit(operation, *args)
    except BaseException:
        semaphore.release()
        raise
    pending = request.app.state.hosted_claim_pending
    pending.add(future)

    def finished(_done: Any) -> None:
        if not loop.is_closed():
            loop.call_soon_threadsafe(lambda: (pending.discard(future), semaphore.release()))

    future.add_done_callback(finished)
    return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=timeout)


async def _body(request: Request) -> dict[str, Any] | None:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        return None
    try:
        if int(request.headers.get("content-length", "0")) > _MAX_BODY:
            return None
    except ValueError:
        return None
    chunks = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > _MAX_BODY:
                    return None
    except TimeoutError:
        return None
    try:
        result = json.loads(chunks)
    except (UnicodeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


async def status(request: Request) -> JSONResponse:
    """Report customer setup independently of capability readiness."""
    service = _service(request)
    if service is None:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    try:
        state = await _bounded_call(request, service.status)
    except Exception as exc:
        logger.warning("hosted setup status unavailable class=%s", type(exc).__name__)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": state}, headers={"Cache-Control": "no-store"})


async def challenge(request: Request) -> JSONResponse:
    """Expose only a short-lived boot-key signature over the current machine facts."""
    service = _service(request)
    if service is None:
        return JSONResponse({"error": "hosted machine unavailable"}, status_code=503)
    try:
        evidence = await _bounded_call(request, service.signed_challenge)
    except Exception as exc:
        logger.warning("hosted machine challenge unavailable class=%s", type(exc).__name__)
        return JSONResponse({"error": "hosted machine unavailable"}, status_code=503)
    return JSONResponse(evidence, headers={"Cache-Control": "no-store"})


async def install_grant(request: Request) -> JSONResponse:
    """Install a signed issuer grant delivered to the machine by cloud control."""
    service = _service(request)
    if service is None:
        return JSONResponse({"error": "hosted account authority unavailable"}, status_code=503)
    envelope = await _body(request)
    if envelope is None:
        return JSONResponse({"error": "invalid grant body"}, status_code=400)
    try:
        await _bounded_call(request, service.install_grant, envelope)
    except Exception as exc:
        logger.warning("hosted grant refused class=%s", type(exc).__name__)
        return JSONResponse({"error": "hosted grant refused"}, status_code=403)
    return JSONResponse({"status": "accepted"}, status_code=202)


async def rekey_intent(request: Request) -> JSONResponse:
    """Present a new ephemeral key bound to the old unclaimed journal head."""
    service = _rekey_service(request)
    if service is None:
        return JSONResponse({"error": "hosted rekey unavailable"}, status_code=503)
    try:
        evidence = await _bounded_call(request, service.signed_intent)
    except Exception as exc:
        logger.warning("hosted rekey intent refused class=%s", type(exc).__name__)
        return JSONResponse({"error": "hosted rekey unavailable"}, status_code=503)
    return JSONResponse(evidence, headers={"Cache-Control": "no-store"})


async def install_rekey(request: Request) -> JSONResponse:
    """Accept only the issuer's endorsement of the pending local rekey."""
    service = _rekey_service(request)
    if service is None:
        return JSONResponse({"error": "hosted rekey unavailable"}, status_code=503)
    envelope = await _body(request)
    if envelope is None:
        return JSONResponse({"error": "invalid rekey body"}, status_code=400)
    try:
        await _bounded_call(request, service.install_rekey, envelope)
    except Exception as exc:
        logger.warning("hosted rekey refused class=%s", type(exc).__name__)
        return JSONResponse({"error": "hosted rekey refused"}, status_code=403)
    return JSONResponse({"status": "accepted"}, status_code=202)


async def claim(request: Request) -> JSONResponse:
    """Create the first account using only the separately issued browser proof."""
    service = _service(request)
    if service is None:
        return JSONResponse({"error": "hosted account authority unavailable"}, status_code=503)
    origin = getattr(request.app.state, "hosted_origin", None)
    if not origin or request.headers.get("origin") != origin:
        return JSONResponse({"error": "setup origin refused"}, status_code=403)
    body = await _body(request)
    if body is None or set(body) != {"customer_secret", "password"}:
        return JSONResponse({"error": "invalid claim body"}, status_code=400)
    secret, password = body["customer_secret"], body["password"]
    if not isinstance(secret, str) or not isinstance(password, str):
        return JSONResponse({"error": "invalid claim body"}, status_code=400)
    if len(secret) > 128 or not 12 <= len(password) <= 1024:
        return JSONResponse({"error": "invalid claim body"}, status_code=400)
    try:
        user = await _bounded_call(request, service.claim, secret, password, timeout=30)
    except HostedBusyError:
        return JSONResponse({"error": "setup is busy"}, status_code=429)
    except TimeoutError:
        return JSONResponse(
            {"status": "claim_pending"}, status_code=202,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        logger.warning("hosted first claim refused class=%s", type(exc).__name__)
        return JSONResponse({"error": "hosted first claim refused"}, status_code=403)
    return JSONResponse(
        {"status": "setup_complete", "email": user.email},
        headers={"Cache-Control": "no-store"},
    )


ROUTES = [
    ("/api/setup/challenge", challenge, ["GET"]),
    ("/api/setup/rekey-intent", rekey_intent, ["GET"]),
    ("/api/setup/rekey", install_rekey, ["POST"]),
    ("/api/setup/status", status, ["GET"]),
    ("/api/setup/grant", install_grant, ["POST"]),
    ("/api/setup/claim", claim, ["POST"]),
]
