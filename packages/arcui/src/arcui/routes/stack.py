"""``POST /api/stack/restart`` — operator-gated restart of the whole stack.

This is the UI twin of the ``arc restart`` CLI command. arcui *is* the
``arc.service`` process, so it cannot orchestrate its own restart in-band: the
moment the app unit is torn down, anything running inside its cgroup dies with
it. So this route does not run the restart — it launches ``arc restart`` inside
its **own transient systemd unit** (``systemd-run --user``), which lives in a
separate cgroup and therefore survives ``arc.service`` stopping and starting.
That detached command is the single source of truth for restart order (it
brings the app unit back, waits for health, then restarts the companions).

``with_db`` (request body) adds ``--with-db``, which also bounces the two
PostgreSQL containers. Operator-gated and audited. The launcher argv is
overridable by ``ARC_STACK_RESTART_COMMAND`` for non-systemd deployments; the
default targets the per-user systemd manager the deploy scripts install into.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _arc_bin() -> str:
    """The ``arc`` console script beside the running interpreter.

    arcui runs from the runtime venv, so ``arc`` is the interpreter's sibling.
    Falls back to the bare name (resolved on PATH by systemd-run) if absent.
    """
    candidate = Path(sys.executable).with_name("arc")
    return str(candidate) if candidate.exists() else "arc"


def _restart_argv(*, with_db: bool) -> list[str]:
    """Build the detached launcher argv for one stack restart.

    ``ARC_STACK_RESTART_COMMAND`` overrides the whole launcher (space-split); the
    ``restart``/``--with-db`` verb is appended to whatever it names, so a custom
    launcher still receives the flag. The default runs ``arc restart`` in a
    uniquely named transient user unit that outlives ``arc.service``.
    """
    override = os.environ.get("ARC_STACK_RESTART_COMMAND")
    if override:
        launcher = shlex.split(override)
    else:
        unit = f"arc-stack-restart-{time.strftime('%Y%m%d-%H%M%S')}"
        launcher = ["systemd-run", "--user", "--collect", f"--unit={unit}", _arc_bin(), "restart"]
    return [*launcher, "--with-db"] if with_db else launcher


def _spawn_restart(argv: list[str]) -> None:
    """Launch the restart in a detached session. A seam for tests to patch.

    The transient unit lives in its own cgroup, so it keeps running after this
    process (``arc.service``) is stopped. ``start_new_session`` detaches it from
    arcui's controlling session as well.
    """
    # Operator-gated route; argv is built from module constants + a boolean,
    # never from free-form request input.
    subprocess.Popen(argv, start_new_session=True)  # noqa: S603


async def restart_stack_route(request: Request) -> JSONResponse:
    """Trigger a full-stack restart (operator only)."""
    if not _is_operator(request):
        return _error("restarting the stack requires operator mode.", 403)

    with_db = False
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = None
    if isinstance(body, dict):
        with_db = bool(body.get("with_db"))

    argv = _restart_argv(with_db=with_db)
    emit_mutation_audit(
        request,
        target="stack",
        operation="restart",
        outcome="allow",
        detail=f"with_db={with_db} :: {' '.join(argv)}",
    )
    try:
        _spawn_restart(argv)
    except OSError as exc:
        return _error(f"could not trigger restart: {exc}", 500)
    return JSONResponse(
        {
            "restarting": True,
            "with_db": with_db,
            "message": "Stack restarting — the dashboard will reconnect shortly.",
        }
    )


routes = [
    Route("/api/stack/restart", restart_stack_route, methods=["POST"]),
]

__all__ = ["restart_stack_route", "routes"]
