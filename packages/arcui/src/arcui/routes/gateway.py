"""``POST /api/gateway/restart`` — operator-gated gateway restart.

The last mile of the in-browser Telegram connect flow (and any config change that the
gateway only reads at startup): let an operator restart the service from the UI instead
of the terminal. arcui *is* the ``arc.service`` process, so it cannot restart itself
in-band — it spawns a **detached** restart that fires after the HTTP response has flushed,
then systemd stops and starts the unit (the browser reconnects when it comes back).

The restart command is ``ARC_RESTART_COMMAND`` (default ``systemctl --user restart
--no-block arc.service``) — overridable for non-systemd deployments. Operator-gated and
audited; it restarts the whole fleet, so it is deliberately a two-step confirm in the UI.
"""

from __future__ import annotations

import os
import subprocess

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

_DEFAULT_RESTART_COMMAND = "systemctl --user restart --no-block arc.service"


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _spawn_restart(command: str) -> None:
    """Spawn the restart in a detached session, delayed so the response flushes first.

    A seam for tests to patch — the real path shells out once, non-blocking, and returns
    immediately; the delay lets Starlette send the 200 before systemd tears the unit down.
    """
    # Operator-gated route; `command` comes from trusted env/default, never request input.
    # The delayed shell one-liner lets the HTTP 200 flush before systemd tears the unit down.
    subprocess.Popen(  # noqa: S603 — trusted, operator-gated command, not user input
        ["/bin/sh", "-c", f"sleep 1; {command}"],
        start_new_session=True,
    )


async def restart_gateway_route(request: Request) -> JSONResponse:
    """Trigger a gateway restart (operator only)."""
    if not _is_operator(request):
        return _error("restarting the gateway requires operator mode.", 403)

    command = os.environ.get("ARC_RESTART_COMMAND", _DEFAULT_RESTART_COMMAND)
    emit_mutation_audit(
        request, target="gateway", operation="restart", outcome="allow", detail=command
    )
    try:
        _spawn_restart(command)
    except OSError as exc:
        return _error(f"could not trigger restart: {exc}", 500)
    return JSONResponse(
        {"restarting": True, "message": "Gateway restarting — the UI will reconnect shortly."}
    )


routes = [
    Route("/api/gateway/restart", restart_gateway_route, methods=["POST"]),
]

__all__ = ["restart_gateway_route", "routes"]
