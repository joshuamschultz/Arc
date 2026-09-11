"""``arc restart`` — one ordered restart of the whole running stack.

This is the single source of truth for restart ORDER. Both entry points use it:
``arc restart`` on the command line runs it directly, and the arcui operator
button launches ``arc restart`` inside its own transient systemd unit (so the
restart outlives the ``arc.service`` process it is about to kill).

Why a command and not just ``systemctl restart arc.service``: a running node is
more than one unit. NATS is a child of ``arc.service`` (it cycles for free), but
the companion services — the CONNECT forward, the voice client, the Hermes
gateway — are their own units, and the two PostgreSQL containers are their own
Docker containers. "Restart everything" means restarting them in an order that
brings dependencies up before the things that need them, and confirming the
dashboard actually answers before declaring success.

Order::

    [--with-db]   restart the Docker PostgreSQL containers, wait for ready
    arc.service   restart (brings NATS + the whole embedded fleet back), wait /api/health
    companions    restart arc-connect-forward, arc-voice, hermes-gateway

The database bounce is opt-in (``--with-db``): a routine "the fleet is on a
stale credential / config" restart wants the fast services-only path and must
not drop the store and memory index under everything for no reason.

Unit and container names are overridable by env for non-default deployments;
the command shells ``systemctl --user`` and ``docker``, so it targets the
per-user systemd manager the deploy scripts install into.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time

from arccli.commands._shared import err as _err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

#: The unit that carries ArcUI, the embedded gateway, and the whole fleet.
#: Restarting it also cycles the NATS child it spawns.
APP_UNIT = os.environ.get("ARC_SERVICE_UNIT", "arc.service")

#: Own-unit companions, restarted after the app unit is healthy. Space-separated
#: so a deployment can trim the list (a node without voice, say) via one env.
COMPANION_UNITS = tuple(
    unit
    for unit in os.environ.get(
        "ARC_COMPANION_UNITS",
        "arc-connect-forward.service arc-voice.service hermes-gateway.service",
    ).split()
    if unit
)

#: The Docker PostgreSQL containers, matched to the deploy scripts' own defaults.
DB_CONTAINERS = (
    os.environ.get("ARCSTORE_PG_CONTAINER", "arcstore-postgres"),
    os.environ.get("ARC_MEMORY_PG_CONTAINER", "arc-memory-postgres"),
)

#: How long to wait for the dashboard to answer after the app unit restarts.
HEALTH_TIMEOUT_S = 90.0

#: How long to wait for a database container to accept connections again.
DB_READY_TIMEOUT_S = 30.0


def _ui_port() -> int:
    raw = os.environ.get("ARC_UI_PORT", "8420")
    try:
        return int(raw)
    except ValueError:
        return 8420


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one ``systemctl --user`` invocation, capturing output."""
    # Trusted argv built from module constants, never request/user input.
    return subprocess.run(  # noqa: S603
        ["systemctl", "--user", *args],  # noqa: S607 — resolved on PATH (systemd --user node)
        capture_output=True,
        text=True,
        check=False,
    )


def _docker_restart(container: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, not user input
        ["docker", "restart", container],  # noqa: S607 — docker resolved on PATH
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_db_ready(container: str, *, timeout: float) -> bool:
    """Poll ``pg_isready`` inside the container until it accepts connections."""
    deadline = time.monotonic() + timeout
    while True:
        probe = subprocess.run(  # noqa: S603 — fixed argv, not user input
            ["docker", "exec", container, "pg_isready", "-q"],  # noqa: S607 — docker on PATH
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _restart_databases(rows: list[list[str]]) -> bool:
    """Bounce both Postgres containers and wait for each to accept connections.

    Returns True only when every container restarted and came back ready.
    """
    if shutil.which("docker") is None:
        rows.append(["databases", "SKIPPED", "docker not found on PATH"])
        return False
    whole = True
    for container in DB_CONTAINERS:
        outcome = _docker_restart(container)
        if outcome.returncode != 0:
            rows.append([container, "FAILED", outcome.stderr.strip() or "docker restart failed"])
            whole = False
            continue
        if _wait_db_ready(container, timeout=DB_READY_TIMEOUT_S):
            rows.append([container, "ready", "restarted, accepting connections"])
        else:
            rows.append([container, "SLOW", f"restarted, not ready in {DB_READY_TIMEOUT_S:.0f}s"])
            whole = False
    return whole


def _restart_app(rows: list[list[str]], *, wait: bool) -> bool:
    """Restart the app unit and (unless ``wait`` is False) confirm health."""
    outcome = _systemctl("restart", APP_UNIT)
    if outcome.returncode != 0:
        rows.append([APP_UNIT, "FAILED", outcome.stderr.strip() or "systemctl restart failed"])
        return False
    if not wait:
        rows.append([APP_UNIT, "restarted", "health not awaited (--no-wait)"])
        return True

    from arccli.commands.up import probe_health

    health = probe_health("127.0.0.1", _ui_port(), timeout=HEALTH_TIMEOUT_S)
    if health.up:
        rows.append([APP_UNIT, "healthy", "dashboard answered /api/health"])
        return True
    rows.append([APP_UNIT, "UNHEALTHY", health.detail])
    return False


def _restart_companions(rows: list[list[str]]) -> bool:
    """Restart each companion unit, continuing past one that fails."""
    whole = True
    for unit in COMPANION_UNITS:
        outcome = _systemctl("restart", unit)
        if outcome.returncode == 0:
            rows.append([unit, "restarted", ""])
        else:
            rows.append([unit, "FAILED", outcome.stderr.strip() or "systemctl restart failed"])
            whole = False
    return whole


def restart_handler(args: list[str]) -> None:
    """Entry point the command registry calls for ``arc restart``."""
    parser = argparse.ArgumentParser(prog="arc restart", description="Restart the whole stack.")
    parser.add_argument(
        "--with-db",
        action="store_true",
        help="Also bounce the PostgreSQL containers (store + memory index).",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Do not wait for the dashboard health check after restarting.",
    )
    parsed = parser.parse_args(args)

    if shutil.which("systemctl") is None:
        _err("arc restart: systemctl not found — this command targets a systemd --user node.")
        raise SystemExit(1)

    rows: list[list[str]] = []
    whole = True

    if parsed.with_db:
        _out("→ restarting databases…")
        whole = _restart_databases(rows) and whole

    _out(f"→ restarting {APP_UNIT} (NATS + fleet)…")
    app_ok = _restart_app(rows, wait=not parsed.no_wait)
    whole = app_ok and whole

    # Companions ride on a live app; only restart them once it is back.
    if app_ok:
        _out("→ restarting companion services…")
        whole = _restart_companions(rows) and whole
    else:
        for unit in COMPANION_UNITS:
            rows.append([unit, "SKIPPED", f"{APP_UNIT} did not come back healthy"])
        whole = False

    _out()
    _print_table(["Component", "Status", "Detail"], rows)

    if whole:
        _out("\n✓ stack restarted")
    else:
        _err("\n✗ stack restart finished with problems (see table)")
        raise SystemExit(1)


__all__ = ["restart_handler"]
