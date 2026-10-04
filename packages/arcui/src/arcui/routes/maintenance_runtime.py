"""Settings -> Maintenance -> Updates: see the installed runtimes, switch, roll back.

Runtimes install side by side under ``~/.arc/runtime/<version>/`` behind a
``current`` symlink. This is the browser's twin of ``arc runtime list`` /
``arc runtime activate``: the same :mod:`arctrust.paths` functions, then the same
detached stack restart ``POST /api/stack/restart`` runs, so the new version is the
one that comes back up.

Pulling a new version from a remote is deliberately not here. The page says so in
plain words when nothing newer is installed.

Safety, in the order a request meets it:

* A viewer may read the list. Only an operator may switch (403, audited).
* The version must match :data:`VERSION_NAME` (letters, digits, dot, dash, at most
  :data:`MAX_VERSION_LENGTH` characters) and name an *installed* runtime. A path,
  ``..``, ``current`` or a stray directory is refused before anything is touched.
* If the restart cannot start, the old version is put back, so a failed attempt
  never leaves the box on a runtime it is not running.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctrust import paths
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, emit_read_audit
from arcui.routes.agent_detail.config_files import BodyTooLargeError, _error, read_json_object
from arcui.routes.stack import launch_stack_restart

logger = logging.getLogger("arcui.routes.maintenance_runtime")

#: A runtime version is a bare directory name. Same alphabet the dashboard sends.
VERSION_NAME = re.compile(r"^[0-9A-Za-z.\-]+$")
MAX_VERSION_LENGTH = 64

_NOTHING_NEWER = (
    "Nothing newer is installed on this computer. Arc cannot download a new version "
    "from here yet; a new version has to be installed on the computer first."
)
_NEWER_HINT = "A newer version is already installed. Choose Switch to use it."


def _installed_at(directory: Path) -> str:
    """When this runtime was put on the box: its directory's last change, in UTC."""
    stamp = directory.stat().st_mtime
    return datetime.fromtimestamp(stamp, tz=UTC).isoformat()


def _runtime_rows() -> tuple[list[dict[str, Any]], str | None]:
    """Installed runtimes, oldest first, each tagged against the live one."""
    active = paths.active_runtime_version()
    entries = [
        (p.name, p.stat().st_mtime, _installed_at(p)) for p in paths.installed_runtime_versions()
    ]
    entries.sort(key=lambda item: (item[1], item[0]))
    active_stamp = next((stamp for name, stamp, _ in entries if name == active), None)
    rows: list[dict[str, Any]] = []
    for name, stamp, installed in entries:
        if name == active:
            relation = "active"
        elif active_stamp is None or stamp > active_stamp:
            relation = "newer"
        else:
            relation = "older"
        rows.append(
            {
                "version": name,
                "active": name == active,
                "installed_at": installed,
                "relation": relation,
            }
        )
    return rows, active


async def get_runtime(request: Request) -> JSONResponse:
    """GET /api/maintenance/runtime — what is installed and what is live."""
    rows, active = _runtime_rows()
    newer = any(row["relation"] == "newer" for row in rows)
    emit_read_audit(request, target="runtime", operation="runtime.list", outcome="ok")
    return JSONResponse(
        {
            "active": active,
            "versions": rows,
            "newer_available": newer,
            "note": _NEWER_HINT if newer else _NOTHING_NEWER,
        }
    )


def _refusal(
    request: Request, version: str, detail: str, message: str, status: int
) -> JSONResponse:
    emit_mutation_audit(
        request,
        target=f"runtime:{version[:MAX_VERSION_LENGTH]}",
        operation="runtime.activate",
        outcome="denied",
        detail=detail,
    )
    return _error(message, status)


def _checked_version(request: Request, version: object) -> str | JSONResponse:
    """``version`` when it is a plausible bare name, else the refusal to send."""
    if not isinstance(version, str) or not version:
        return _refusal(request, "", "missing", "Choose the version to switch to.", 400)
    if len(version) > MAX_VERSION_LENGTH or not VERSION_NAME.fullmatch(version):
        return _refusal(
            request,
            version,
            "bad name",
            "That is not a version name. Pick one from the list.",
            400,
        )
    return version


async def _requested_version(request: Request) -> object:
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return None
    return None if body is None else body.get("version")


async def activate_runtime(request: Request) -> JSONResponse:
    """POST /api/maintenance/runtime/activate — switch or roll back, then restart (operator)."""
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target="runtime",
            operation="runtime.activate",
            outcome="denied",
            detail="not an operator",
        )
        return _error("Only an operator can switch versions.", 403)

    version = _checked_version(request, await _requested_version(request))
    if isinstance(version, JSONResponse):
        return version

    installed = {p.name for p in paths.installed_runtime_versions()}
    if version not in installed:
        return _refusal(
            request, version, "not installed", f"Version {version} is not installed here.", 404
        )

    previous = paths.active_runtime_version()
    if version == previous:
        return _error(f"Version {version} is already the one in use.", 409)

    try:
        paths.activate_runtime(version)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return _refusal(request, version, type(exc).__name__, "Could not switch versions.", 500)

    try:
        launch_stack_restart()
    except OSError:
        _put_back(previous)
        emit_mutation_audit(
            request,
            target=f"runtime:{version}",
            operation="runtime.activate",
            outcome="error",
            detail=f"restart did not start; kept {previous}",
        )
        return _error(
            "Could not restart Arc, so nothing was changed. "
            "The version in use is the same as before.",
            500,
        )

    emit_mutation_audit(
        request,
        target=f"runtime:{version}",
        operation="runtime.activate",
        outcome="applied",
        detail=f"from={previous}",
    )
    return JSONResponse(
        {
            "restarting": True,
            "version": version,
            "message": f"Switched to {version}. Arc is restarting.",
        }
    )


def _put_back(previous: str | None) -> None:
    """Restore the version that was live before a failed switch."""
    if previous is None:
        return
    try:
        paths.activate_runtime(previous)
    except (ValueError, FileNotFoundError, OSError):
        logger.exception("could not put the previous runtime back")


routes = [
    Route("/api/maintenance/runtime", get_runtime, methods=["GET"]),
    Route("/api/maintenance/runtime/activate", activate_runtime, methods=["POST"]),
]

__all__ = ["activate_runtime", "get_runtime", "routes"]
