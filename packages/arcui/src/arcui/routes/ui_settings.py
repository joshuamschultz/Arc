"""Settings → Access: the dashboard's public address and its own https certificate.

``GET/PUT /api/settings/public-address`` — the origin OAuth providers send a browser
back to. A save takes effect on the next sign-in (the redirect URI is derived from
the stored value on every use, never from this request's ``Host``).

``GET/PUT/DELETE /api/settings/tls`` — an operator-provided certificate and key so
the dashboard can serve https on a LAN or tailnet. Takes effect after a restart
(Settings already offers one). The key goes to custody; it is never echoed.

Reads are open to any signed-in role; writes are operator-only and audited.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.public_address import PublicAddress, effective_tier
from arcui.routes.agent_detail.config_files import (
    BodyTooLargeError,
    _error,
    read_json_object,
)
from arcui.ui_tls import TLS_REQUIRED, TlsStatus, remove_tls, save_tls, tls_status

logger = logging.getLogger("arcui.routes.ui_settings")

_TAILSCALE_TIMEOUT_SECONDS = 2.0
_LOOPBACK_PROXY_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _public_address(request: Request) -> PublicAddress:
    address: PublicAddress = request.app.state.public_address
    return address


# --- public address ---------------------------------------------------------


async def _tailscale_serve_status() -> dict[str, Any] | None:
    """``tailscale serve status --json``, or ``None`` when there is no usable tailscale."""
    binary = shutil.which("tailscale")
    if binary is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "serve",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), _TAILSCALE_TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        return None
    try:
        parsed = json.loads(out or b"{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _proxies_to_port(handlers: Any, port: int) -> bool:
    if not isinstance(handlers, dict):
        return False
    targets = {f"http://{host}:{port}" for host in _LOOPBACK_PROXY_HOSTS}
    return any(
        isinstance(handler, dict) and str(handler.get("Proxy", "")).rstrip("/") in targets
        for handler in handlers.values()
    )


def _tailscale_origins(status: dict[str, Any] | None, port: int) -> list[str]:
    """https origins ``tailscale serve`` publishes in front of this dashboard's port."""
    web = (status or {}).get("Web")
    if not isinstance(web, dict):
        return []
    origins: list[str] = []
    for host_port, site in web.items():
        if not isinstance(site, dict) or not _proxies_to_port(site.get("Handlers"), port):
            continue
        host, _, listen = str(host_port).rpartition(":")
        origins.append(f"https://{host}" if listen == "443" else f"https://{host_port}")
    return origins


async def _suggestions(request: Request) -> list[dict[str, str]]:
    """Origins Arc can see in front of it. Offered to the operator, never applied."""
    port = _public_address(request).ui_port
    status = await _tailscale_serve_status()
    return [{"source": "tailscale", "url": url} for url in _tailscale_origins(status, port)]


def _address_body(address: PublicAddress, stored: str | None) -> dict[str, Any]:
    tier = effective_tier()
    return {
        "public_base_url": stored,
        "redirect_uri": address.redirect_uri(),
        "tier": tier,
        "https_required": tier != "personal",
    }


async def get_public_address(request: Request) -> JSONResponse:
    """GET /api/settings/public-address — the stored address and the redirect it gives."""
    address = _public_address(request)
    try:
        body = _address_body(address, address.current())
    except arcagent.ExtensionError as exc:
        return _error(exc.message, 409)
    body["suggestions"] = await _suggestions(request)
    return JSONResponse(body)


async def put_public_address(request: Request) -> JSONResponse:
    """PUT /api/settings/public-address — ``{"public_base_url": str | null}``. Operator only."""
    if not _operator(request):
        return _error("Operator role required", 403)
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if body is None or "public_base_url" not in body:
        return _error("Body must be a JSON object with 'public_base_url'", 400)
    raw = body["public_base_url"]
    if raw is not None and not isinstance(raw, str):
        return _error("'public_base_url' must be a string or null", 400)
    address = _public_address(request)
    try:
        stored = address.save(raw)
    except arcagent.ExtensionError as exc:
        _audit_address(request, "denied", exc.code)
        return _error(exc.message, 400)
    _audit_address(request, "applied", stored or "cleared")
    body_out = _address_body(address, stored)
    body_out["suggestions"] = []
    return JSONResponse(body_out)


def _audit_address(request: Request, outcome: str, detail: str) -> None:
    emit_mutation_audit(
        request,
        target="ui:public_address",
        operation="ui.public_address.write",
        outcome=outcome,
        detail=detail,
    )


# --- dashboard TLS ------------------------------------------------------------


def _tls_body(request: Request, status: TlsStatus) -> dict[str, Any]:
    return {
        "configured": status.configured,
        "active": bool(request.app.state.ui_tls_active),
        "required": effective_tier() == "federal",
        "subject": status.subject,
        "not_after": status.not_after,
        "dns_names": list(status.dns_names),
    }


async def get_tls(request: Request) -> JSONResponse:
    """GET /api/settings/tls — the stored certificate's public facts. Never key material."""
    return JSONResponse(_tls_body(request, await asyncio.to_thread(tls_status)))


async def put_tls(request: Request) -> JSONResponse:
    """PUT /api/settings/tls — ``{"cert_pem", "key_pem"}``. Operator only; next start serves it."""
    if not _operator(request):
        return _error("Operator role required", 403)
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    cert = body.get("cert_pem") if body is not None else None
    key = body.get("key_pem") if body is not None else None
    if not isinstance(cert, str) or not isinstance(key, str):
        return _error("Body must be a JSON object with 'cert_pem' and 'key_pem'", 400)
    try:
        status = await asyncio.to_thread(save_tls, cert, key)
    except arcagent.ExtensionError as exc:
        _audit_tls(request, "ui.tls.write", "denied", exc.code)
        return _error(exc.message, 400)
    _audit_tls(request, "ui.tls.write", "applied", status.subject or "")
    return JSONResponse({**_tls_body(request, status), "restart_required": True})


async def delete_tls(request: Request) -> JSONResponse:
    """DELETE /api/settings/tls — forget the certificate. Refused at federal (409)."""
    if not _operator(request):
        return _error("Operator role required", 403)
    try:
        status = await asyncio.to_thread(remove_tls, tier=effective_tier())
    except arcagent.ExtensionError as exc:
        _audit_tls(request, "ui.tls.delete", "denied", exc.code)
        return _error(exc.message, 409 if exc.code == TLS_REQUIRED else 400)
    _audit_tls(request, "ui.tls.delete", "applied", "")
    return JSONResponse({**_tls_body(request, status), "restart_required": True})


def _audit_tls(request: Request, operation: str, outcome: str, detail: str) -> None:
    emit_mutation_audit(
        request, target="ui:tls", operation=operation, outcome=outcome, detail=detail
    )


routes = [
    Route("/api/settings/public-address", get_public_address, methods=["GET"]),
    Route("/api/settings/public-address", put_public_address, methods=["PUT"]),
    Route("/api/settings/tls", get_tls, methods=["GET"]),
    Route("/api/settings/tls", put_tls, methods=["PUT"]),
    Route("/api/settings/tls", delete_tls, methods=["DELETE"]),
]
