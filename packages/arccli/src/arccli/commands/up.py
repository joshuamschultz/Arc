"""``arc up`` — one supervised bring-up of the whole stack.

Four stages, each of which reports what it found and refuses to hide a problem
behind a started process::

    preflight   nats-server, operator key, team root, data dir
    modules     install every module an agent's config enables but the
                deployment has not materialized
    verify      health, NATS, and a per-agent capability table
    start       the embedded stack, through the same path `arc ui start` uses

The command exists because of one specific failure shape. Modules ship as
separately signed bundles, so a ``git pull && uv sync && systemctl restart``
leaves an agent with **zero** modules — and that agent still boots, still
answers chat, and still looks healthy while its scheduler never fires and its
tasks never dispatch. Nothing crashes, so nothing is noticed.

Verify therefore runs *before* start rather than after it. A deployment that
would come up degraded is not started at all and the command exits non-zero, so
"started but degraded" never gets the chance to look like success. The one
check that genuinely cannot precede the server — does ``/api/health`` answer —
is made from a daemon thread beside it and printed when the server is up.

There is no business logic here. Preflight reads seams (``shutil.which``,
``operator_public_key``, ``arcstore.resolve_data_dir``); the module stage calls
``arccli.commands.module.install_module_for_agent``, the same verify →
materialize → copy → enable path ``arc module install`` runs; the start stage
invokes ``arccli.commands.ui.ui_handler`` in-process rather than shelling out
to ``arc``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from arccli.commands._shared import err as _err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

#: How long the post-start watcher waits for the server to answer /api/health.
HEALTH_TIMEOUT_S = 60.0

#: Bind addresses meaning "every interface" — not dialable as a destination.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "*", ""})  # noqa: S104


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One preflight line: what was checked, whether it passed, what was found."""

    name: str
    ok: bool
    detail: str

    @property
    def status(self) -> str:
        return "ok" if self.ok else "FAIL"


@dataclass(frozen=True)
class Service:
    """One probed service. ``up`` is observed state, never a verdict.

    Before a start, NATS and the dashboard are legitimately down — the bring-up
    is what starts them — so these rows are reported and never gate anything.
    The preflight already proved the prerequisites that make starting possible.
    """

    name: str
    up: bool
    detail: str

    @property
    def state(self) -> str:
        return "up" if self.up else "down"


@dataclass(frozen=True)
class AgentState:
    """What one agent's config asks for, against what the deployment has.

    ``missing`` is the whole point: a module name the config enables that is
    not materialized at the deployment module root. Such a module cannot load,
    cannot register a tool, and cannot configure a runtime — the agent simply
    runs without the capability and says nothing about it.
    """

    agent_id: str
    root: Path
    enabled: tuple[str, ...]
    missing: tuple[str, ...]
    error: str = ""

    @property
    def degraded(self) -> bool:
        return bool(self.missing) or bool(self.error)


# ---------------------------------------------------------------------------
# Team root + agent discovery
# ---------------------------------------------------------------------------


def resolve_team_root(team_root_arg: str | None) -> Path | None:
    """Resolve ``--team-root`` exactly the way ``arc ui start`` does.

    Both must agree: a bring-up that verified one directory and then served
    another would report on agents that never started.
    """
    if team_root_arg:
        return Path(team_root_arg).expanduser().resolve()
    default = Path.cwd() / "team"
    return default.resolve() if default.is_dir() else None


def discover_agents(team_root: Path) -> list[tuple[str, Path]]:
    """Return ``(agent_id, agent_dir)`` for every agent under ``team_root``.

    Read through ``arcgateway.team_roster`` — the same roster the dashboard and
    ``arc module install --agent`` resolve names against — so an id printed here
    is an id the operator can paste into the remedy command underneath it.
    """
    from arcgateway import team_roster

    return [
        (entry.agent_id, Path(entry.workspace_path))
        for entry in team_roster.list_team(team_root=team_root, online_ids=set())
    ]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _check_nats() -> Check:
    """``nats-server`` must be on PATH: arcteam spawns it, it is not a wheel dep.

    Without it every agent silently loses team messaging and task dispatch while
    the process itself stays healthy — exactly the degradation this command
    exists to make loud, so a missing binary stops the bring-up.
    """
    found = shutil.which("nats-server")
    if found:
        return Check("nats-server on PATH", True, found)
    return Check(
        "nats-server on PATH",
        False,
        "not found — team messaging and task dispatch would be dead. Install it from "
        "https://github.com/nats-io/nats-server/releases onto PATH, or run "
        "scripts/deploy-node.sh which does it for you.",
    )


def _check_operator_key() -> Check:
    """An operator key must already exist. This never mints one.

    The operator key is the trust anchor a bundle, a blueprint, and a prompt
    overlay are pinned to, and the identity the audit chain is signed with.
    Minting one here would hand a bring-up the authority to decide what this
    deployment trusts — an unpinned operator is no operator.
    """
    from arctrust import arc_home

    from arccli.commands.operator import operator_public_key

    home = arc_home()
    try:
        public_key = operator_public_key(home)
    except Exception as exc:  # reason: a tampered key is a reportable failure, not a crash
        return Check("operator key", False, f"unreadable: {type(exc).__name__}: {exc}")
    if public_key is None:
        return Check(
            "operator key",
            False,
            f"none under {home} — module signatures cannot be verified and the audit "
            "chain has no signer. Create one with `arc init`, the setup wizard that "
            "mints and pins it.",
        )
    return Check("operator key", True, f"present under {home}")


def _check_team_root(team_root: Path | None) -> Check:
    """The team root must resolve and hold at least one agent."""
    if team_root is None:
        return Check(
            "team root",
            False,
            "not resolved — pass --team-root <dir>, or run from a directory holding team/.",
        )
    if not team_root.is_dir():
        return Check("team root", False, f"{team_root} does not exist")
    agents = discover_agents(team_root)
    if not agents:
        return Check(
            "team root",
            False,
            f"{team_root} holds no agent (no <name>/arcagent.toml). Create one with "
            "`arc agent create <name> --dir team`.",
        )
    names = ", ".join(agent_id for agent_id, _ in agents)
    return Check("team root", True, f"{team_root} — {len(agents)} agent(s): {names}")


def _check_data_dir() -> Check:
    """The arcstore data dir must exist and be writable by this user."""
    from arcstore import resolve_data_dir

    data_dir = resolve_data_dir(None)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check("data dir", False, f"{data_dir} cannot be created: {exc}")
    if not os.access(data_dir, os.W_OK):
        return Check("data dir", False, f"{data_dir} is not writable by this user")
    return Check("data dir", True, str(data_dir))


def preflight(team_root: Path | None) -> list[Check]:
    """Run every precondition check. Nothing here fixes anything."""
    return [_check_nats(), _check_operator_key(), _check_team_root(team_root), _check_data_dir()]


# ---------------------------------------------------------------------------
# Module bootstrap
# ---------------------------------------------------------------------------


def agent_states(team_root: Path) -> list[AgentState]:
    """Read each agent's config and diff its enabled modules against the disk.

    ``arcagent.discover_modules()`` is the agent's own predicate for "this
    module will load": it is what builds the ``module:<name>`` scan root and
    what gates runtime configuration. Asking the same question here means the
    table cannot claim a capability the agent will not have.
    """
    import arcagent

    present = set(arcagent.discover_modules())
    states: list[AgentState] = []
    for agent_id, agent_root in discover_agents(team_root):
        try:
            config = arcagent.load_config(agent_root / "arcagent.toml")
        except Exception as exc:  # reason: one unreadable config must not hide the rest
            states.append(AgentState(agent_id, agent_root, (), (), f"{type(exc).__name__}: {exc}"))
            continue
        enabled = tuple(sorted(name for name, entry in config.modules.items() if entry.enabled))
        states.append(
            AgentState(
                agent_id=agent_id,
                root=agent_root,
                enabled=enabled,
                missing=tuple(name for name in enabled if name not in present),
            )
        )
    return states


def bootstrap_modules(states: list[AgentState]) -> list[list[str]]:
    """Install every enabled-but-absent module, and report each outcome.

    Returns the rows of the module table: one line per agent for what was
    already there, plus one line per module that had to be installed or could
    not be. Refusals are collected rather than raised — an operator fixing a
    fleet needs the whole picture, and verify refuses to start on whatever is
    still missing afterwards.
    """
    from arccli.commands.module import ModuleInstallError, install_module_for_agent

    rows: list[list[str]] = []
    for state in states:
        if state.error:
            rows.append([state.agent_id, "-", "UNREADABLE", state.error])
            continue
        already = [name for name in state.enabled if name not in state.missing]
        rows.append(
            [
                state.agent_id,
                ", ".join(already) or "-",
                "present" if already else "-",
                f"{len(already)} already materialized",
            ]
        )
        for module in state.missing:
            try:
                detail = install_module_for_agent(
                    module, agent_root=state.root, agent_id=state.agent_id
                )
            except ModuleInstallError as exc:
                rows.append([state.agent_id, module, "REFUSED", str(exc)])
                continue
            rows.append([state.agent_id, module, "installed", detail])
    return rows


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _dialable(host: str) -> str:
    """Map a wildcard bind address to a host a probe can actually connect to."""
    return "127.0.0.1" if host in _WILDCARD_HOSTS else host


def probe_nats() -> Service:
    """Try a TCP connect to the resolved broker url."""
    from arcteam.config import default_nats_url

    url = default_nats_url()
    parts = urlsplit(url)
    host, port = _dialable(parts.hostname or "127.0.0.1"), parts.port or 4222
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return Service("NATS", True, f"reachable at {url}")
    except OSError as exc:
        return Service("NATS", False, f"nothing listening at {url} ({exc.strerror or exc})")


def probe_health(host: str, port: int, *, timeout: float = 0.0) -> Service:
    """Poll ``GET /api/health`` until it answers or ``timeout`` elapses.

    ``timeout=0`` is a single attempt — what the pre-start report wants: it
    observes whether something is already serving on that port, and nothing
    more.
    """
    url = f"http://{_dialable(host)}:{port}/api/health"
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            # S310: the scheme is the "http" literal above and the host is the
            # loopback/bind address this same command serves on — not user input.
            with urllib.request.urlopen(url, timeout=2.0) as response:  # noqa: S310
                if response.status == 200:
                    return Service("dashboard health", True, f"{url} responded 200")
                last = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
        if time.monotonic() >= deadline:
            return Service("dashboard health", False, f"{url}: {last}")
        time.sleep(0.5)


def print_verify(states: list[AgentState], services: list[Service]) -> bool:
    """Print the verify tables. Returns True when the deployment is whole.

    A config-enabled module that is not present is a LOUD line, never a
    footnote: a MISSING row, repeated on stderr with the exact remedy, and it is
    what turns the exit code non-zero. Service rows are observed state and never
    gate — before a start they are legitimately down.
    """
    rows: list[list[str]] = []
    for state in states:
        if state.error:
            rows.append([state.agent_id, "-", "UNREADABLE"])
            continue
        if not state.enabled:
            rows.append([state.agent_id, "-", "no modules enabled"])
        for module in state.enabled:
            state_label = "MISSING" if module in state.missing else "present"
            rows.append([state.agent_id, module, state_label])
    _print_table(["Agent", "Module", "State"], rows)
    _out("")
    _print_table(["Service", "State", "Detail"], [[s.name, s.state, s.detail] for s in services])
    _out("  (services are reported as observed; before a start they are down by design)")
    # The DEGRADED lines below go to stderr and must land UNDER the table they
    # explain. Two streams to one terminal only interleave in write order if the
    # buffered one is flushed first.
    sys.stdout.flush()

    whole = True
    for state in states:
        if state.error:
            whole = False
            _err(f"DEGRADED: {state.agent_id} — config unreadable: {state.error}")
        for module in state.missing:
            whole = False
            _err(
                f"DEGRADED: {state.agent_id} enables module {module!r} but it is not "
                "installed — the agent would run without that capability and say nothing. "
                f"Install it with: arc module install {module} --agent {state.agent_id}"
            )
    return whole


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def ui_start_argv(args: argparse.Namespace, team_root: Path) -> list[str]:
    """Build the argv ``arc ui start`` parses for itself.

    An argv rather than a hand-built namespace, so every default this command
    does not set comes from ``arc ui start``'s own parser and the two can never
    drift apart. ``team_root`` is the already-resolved path, so the server loads
    exactly the agents the stages above reported on.
    """
    argv = [
        "start",
        "--team-root",
        str(team_root),
        "--port",
        str(args.port),
        "--host",
        args.host,
    ]
    if args.gateway_config:
        argv += ["--gateway-config", args.gateway_config]
    if args.no_browser:
        argv.append("--no-browser")
    if args.verbose:
        argv.append("--verbose")
    return argv


def watch_health(host: str, port: int) -> None:
    """Print the health verdict once the server answers, from a daemon thread.

    ``arc ui start`` blocks for the process lifetime, so the only way to prove
    the server actually came up is to ask from beside it. A daemon thread dies
    with the process and can never hold a shutdown open.
    """

    def _watch() -> None:
        service = probe_health(host, port, timeout=HEALTH_TIMEOUT_S)
        if service.up:
            _out(f"  Verified: {service.detail}")
            # Flushed explicitly: this line is written from a thread while the
            # main one is blocked inside uvicorn, so nothing else will drain
            # stdout's buffer before the process is signalled to stop.
            sys.stdout.flush()
        else:
            _err(f"  HEALTH FAILED after {HEALTH_TIMEOUT_S:.0f}s: {service.detail}")

    threading.Thread(target=_watch, name="arc-up-health", daemon=True).start()


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _up(args: argparse.Namespace) -> None:
    """Run the stages in order, stopping at the first that would degrade."""
    check_only: bool = args.check
    team_root = resolve_team_root(args.team_root)

    _out("Preflight")
    checks = preflight(team_root)
    _print_table(["Check", "Status", "Detail"], [[c.name, c.status, c.detail] for c in checks])
    if team_root is None or not all(check.ok for check in checks):
        sys.stdout.flush()  # keep the stderr verdict under the table it refers to
        _err("\narc up: preflight failed — nothing was started. Fix the FAIL lines above.")
        sys.exit(1)

    _out("\nModules")
    if check_only or args.no_install:
        _out(f"  skipped ({'--check' if check_only else '--no-install'}) — reporting only.")
    else:
        _print_table(
            ["Agent", "Module(s)", "Action", "Detail"], bootstrap_modules(agent_states(team_root))
        )

    _out("\nVerify")
    services = [probe_nats(), probe_health(args.host, args.port)]
    if not print_verify(agent_states(team_root), services):
        _err(
            "\narc up: this deployment would come up degraded — nothing was started. "
            "A degraded stack that looks healthy is the failure this command prevents."
        )
        sys.exit(1)

    if check_only:
        _out("\narc up --check: the deployment is whole. Start it with: arc up")
        return

    _out("\nStart")
    from arccli.commands.ui import ui_handler

    watch_health(args.host, args.port)
    ui_handler(ui_start_argv(args, team_root))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc up",
        description="Bring up the whole stack: preflight, modules, verify, start.",
        add_help=True,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--team-root", dest="team_root", default=None, help="Directory of agents.")
    parser.add_argument("--port", type=int, default=8420, help="Dashboard port.")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind address.")
    parser.add_argument(
        "--gateway-config", dest="gateway_config", default=None, help="Path to gateway.toml."
    )
    parser.add_argument(
        "--no-browser",
        dest="no_browser",
        action="store_true",
        default=False,
        help="Do not auto-open a browser tab on loopback start.",
    )
    parser.add_argument(
        "--no-install",
        dest="no_install",
        action="store_true",
        default=False,
        help="Skip the module bootstrap; report state without changing it.",
    )
    parser.add_argument(
        "--check",
        dest="check",
        action="store_true",
        default=False,
        help="Preflight + verify only. Starts nothing; exits non-zero on any problem.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        dest="verbose",
        action="store_true",
        default=False,
        help="Raise the root logger to INFO once the server starts.",
    )
    return parser


def up_handler(args: list[str]) -> None:
    """Top-level handler for ``arc up [flags]`` (registry dispatch)."""
    _up(_build_parser().parse_args(args))
