"""``arc connector`` — connect this deployment to an external system, and manage it.

SPEC-062 COMP-016, SPEC-064 T-011. Thirteen verbs make up the COMPLETE management
surface (REQ-293): ``available``, ``add``, ``grant``, ``revoke``, ``auth``,
``authorize``, ``host-setup``, ``list``, ``tools``, ``probe``, ``doctor``,
``approve``, and ``remove``. The terminal is sufficient for all of them; the web
panel is a convenience that is required for nothing (D-561).

**A connection is the deployment's; a grant hands it to an agent.** There is no
``--agent`` flag, because an account is not an agent's property: ``add`` connects
it once and ``--agents`` names who gets it, ``grant``/``revoke`` change that
afterwards, and ``list`` answers "who can reach what" for the whole fleet in one
table. An agent nobody has named holds nothing, so creating one can never widen
access.

``authorize`` and ``host-setup`` are the two verbs that touch the machine, and
both stay honest about what they can do: a login only a person can finish is
printed rather than attempted, and an install happens only against the digest the
bundle pins for THIS platform.

This module owns exactly two things the shared path cannot: **asking the
operator**, and the argparse surface. Every declared credential is collected with
``getpass``, which does not echo, and handed straight to
:mod:`arcagent.connections`, which writes it to the per-agent secret store and
nowhere else — never into a config file, a log, a prompt, or model context. There
is deliberately no ``--token``-style flag: a credential on argv lands in shell
history and in the process table, which is the exact exposure the hidden prompt
exists to remove. This is the constraint ``gateway_connect.py`` states in its own
docstring, and it is why connector setup is a CLI/settings action rather than
something an agent could be asked to do in chat (LLM07).

Everything else — the agent's world, the audit chain's lifetime, the secret
backend, the ordering, the rollback, the config write — is
:class:`arcagent.connections.Connections`, so the terminal, the TUI, and the web
drive one path rather than three copies of it (D-587).

Paths are explicit and overridable — ``--extensions-root``, ``--env-file``,
``--arc-dir``, ``--data-dir`` — because an operator running more than one
deployment on a box needs to point a command at one without disturbing another.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

import arcagent
from arctrust.paths import arc_home, arc_team

from arccli.commands._shared import dispatch, err
from arccli.commands._shared import print_json as _print_json
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

# ---------------------------------------------------------------------------
# Seams — module-level so a test can substitute them without patching internals
# ---------------------------------------------------------------------------


def _attachment_factory() -> arcagent.AttachmentFactory | None:
    """How a manifest becomes something probeable. ``None`` means the shipped builder."""
    return None


def _arcstore_opener() -> Callable[[], Awaitable[Any]] | None:
    """Optional ArcStore opener passed into the connection state seam."""
    return None


def _connections(args: argparse.Namespace) -> arcagent.Connections:
    """Bind this deployment to the operator-signed chain, or exit naming what is wrong.

    The chain is described, never held: every verb opens and closes its own through
    :class:`~arcagent.connections.AuditChain`, because a ``WormSink`` keeps an
    exclusive ``flock`` for its lifetime and one left open across a ``getpass``
    prompt would lock every later writer out.

    The chain lives with the operational data (the one ``arc task`` and ``arc
    workflow`` write to); the key that signs it lives in the config dir. Both are
    ``--`` overridable so an operator can point one command at one deployment's
    world without touching another's.
    """
    from arccli.commands.operator import operator_worm_sink

    try:
        world = arcagent.resolve_deployment(
            arc_dir=getattr(args, "arc_dir", None) or arc_home(),
            data_dir=getattr(args, "data_dir", None),
            extensions_root=getattr(args, "extensions_root", None),
            env_file=getattr(args, "env_file", None),
        )
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    return arcagent.Connections(
        world,
        audit=arcagent.AuditChain.opened_by(
            lambda: operator_worm_sink(world.arc_dir, world.data_dir)
        ),
        attachment_factory=_attachment_factory(),
        state_opener=_arcstore_opener(),
    )


def _fail(message: str) -> NoReturn:
    """Report the problem to stderr and stop. Never returns."""
    err(f"arc connector: {message}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _add(args: argparse.Namespace) -> None:
    """Connect one account: prompt, probe, persist and grant only on success."""
    connections = _connections(args)
    world = connections.world
    agents = _agents(args)
    _require_deployment_agents(connections, agents)
    try:
        plan = connections.plan(args.extension, args.name, agents=agents)
        _refuse_unsatisfied_host(plan)
        report = asyncio.run(connections.install(plan, _prompt_secrets(plan), agents=agents))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)

    _out(f"Connected {report.extension} as '{report.instance}'.")
    _out(f"  granted to     : {', '.join(agents) or '(no agent yet — run: arc connector grant)'}")
    _out(f"  approval mode  : {plan.approval_mode}")
    _out(f"  credentials in : {world.env_file}  (owner-only)")
    if report.detail:
        _out(f"  probe          : {report.detail}")
    _out(f"  tools          : {', '.join(report.tools) or '(none served)'}")
    if agents:
        _out("  Restart those agents for the connection to attach.")


def _grant(args: argparse.Namespace) -> None:
    """Hand one connected account to more agents."""
    connections = _connections(args)
    agents = _agents(args)
    _require_deployment_agents(connections, agents)
    try:
        granted = connections.grant(args.instance, agents)
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    _out(f"'{args.instance}' is now granted to: {', '.join(granted.agents) or '(nobody)'}")
    _out("  Restart those agents for the connection to attach.")


def _revoke(args: argparse.Namespace) -> None:
    """Take one connected account back from agents. The account itself is untouched."""
    connections = _connections(args)
    try:
        remaining = connections.revoke(args.instance, _agents(args))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    _out(f"'{args.instance}' is now granted to: {', '.join(remaining.agents) or '(nobody)'}")
    _out("  Restart the agents that lost it; a running agent keeps what it attached.")


def _agents(args: argparse.Namespace) -> list[str]:
    """The ``--agents a,b`` list, split once so every verb reads it the same way."""
    raw = getattr(args, "agents", "") or ""
    return [name.strip() for name in raw.split(",") if name.strip()]


def _require_deployment_agents(connections: arcagent.Connections, agents: Sequence[str]) -> None:
    """Refuse grants to names that are not agents in this deployment's fleet."""
    roster = arc_team(base=connections.world.arc_dir)
    known = (
        {entry.name for entry in roster.iterdir() if entry.is_dir()} if roster.exists() else set()
    )
    missing = [agent for agent in agents if agent not in known]
    if missing:
        _fail(f"no agent named {', '.join(missing)} on this deployment")


def _auth(args: argparse.Namespace) -> None:
    """Authorise this instance — by prompt when Arc holds the credential, by directing
    the operator to the host command when the connector's own binary holds it."""
    connections = _connections(args)
    try:
        plan = connections.plan_for(args.instance)
        if not plan.secrets:
            _direct_host_authorization(asyncio.run(connections.authorization(args.instance)))
            return
        updated = asyncio.run(connections.reauth(plan, _prompt_secrets(plan)))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    env_file = connections.world.env_file
    _out(f"Updated {len(updated)} credential(s) for '{args.instance}' in {env_file}.")
    _out("  Every agent granted this connection uses the new value at its next start.")


#: How each sign-in state reads in a terminal. "not known" is its own line rather
#: than a silence, because the terminal must not imply either answer.
_SIGN_IN_LINE = {
    "signed_in": "SIGNED IN",
    "signed_out": "NOT signed in",
    "unknown": "sign-in not known (this bundle declares no way to check it)",
}


def _direct_host_authorization(auth: arcagent.Authorization) -> None:
    """Print the exact command that authorises a connector whose binary owns its token.

    "declares no credentials; nothing to supply" was true and useless: ``gh`` does
    need authorising, just not by Arc, and an operator who reads that has nowhere
    to go. Every line here is something to type or something already done.

    The sign-in and the probe are printed as separate lines because they are
    separate facts: ``dbxcli version`` answers on a ``dbxcli`` that has never been
    signed in, so "answering" alone once read as a connected account.
    """
    _out(f"{auth.extension} keeps its own credential; Arc never holds one for it.")
    if not auth.hosts:
        _out("  Its manifest names no authorising command — see the bundle's host_requires.")
    for host in auth.hosts:
        _out(f"  Run on this host: {host.command}")
    if auth.token_binary:
        _out(f"  Or let Arc do it: arc connector authorize {auth.instance}")
    detail = f" — {auth.sign_in_detail}" if auth.sign_in_detail else ""
    _out(f"  {_SIGN_IN_LINE[auth.sign_in]}{detail}")
    _out(f"  {'answering' if auth.reachable else 'NOT answering'}: {auth.detail}")


def _authorize(args: argparse.Namespace) -> None:
    """Sign in a connector whose binary holds its own token — honestly, either way.

    A binary that reads a token on stdin is prompted for and run. One whose login
    opens a browser or prints a device code is NOT attempted: the command is
    printed instead, because a terminal that reports a sign-in nobody completed is
    the same lie a fake button tells.

    There is no ``--token`` flag, for the reason this module's docstring gives:
    a credential on argv lands in shell history and in the process table.
    """
    connections = _connections(args)
    try:
        auth = asyncio.run(connections.authorization(args.instance))
        if auth.token_binary:
            token = getpass.getpass(f"Token for {auth.token_binary} (hidden): ")
            auth = asyncio.run(connections.authorize(args.instance, token=token))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)

    _direct_host_authorization(auth)
    if not auth.working:
        sys.exit(1)


def _host_setup(args: argparse.Namespace) -> None:
    """Install the host binaries a bundle pins, verified against its published digest.

    Arc still never runs the manifest's ``instruction`` (REQ-262): the manifest
    names bytes and a digest, the bytes are checked before anything is unpacked,
    and they land in the operator's own ``~/.local/bin``. Anything Arc cannot
    install prints the bundle's own steps and exits non-zero.
    """
    connections = _connections(args)
    try:
        report = asyncio.run(connections.setup_host(args.extension))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)

    _out(report.detail)
    if report.installed:
        return
    if report.manual_steps:
        _out("")
        _out(report.manual_steps)
    sys.exit(1)


def _available(args: argparse.Namespace) -> None:
    """Show every bundle the search path holds — the answer to "what can I connect?".

    Nothing is installed, nothing is written, and nothing is audited — no verdict
    is taken here, and the refusal an operator acts on is taken (and recorded) by
    ``add``.
    """
    arc_dir = Path(args.arc_dir).expanduser() if args.arc_dir else None
    try:
        tier = arcagent.deployment_tier(arc_dir)
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    roots = arcagent.resolve_roots(arc_dir, extensions_root=args.extensions_root)
    entries = arcagent.catalog(roots=roots, tier=tier)

    if args.json:
        _print_json([_entry_json(entry) for entry in entries])
        return
    if not entries:
        _out("No extension bundles found.")
        _out(f"  searched: {_roots_line(roots)}")
        return
    _print_table(
        ["Name", "Version", "Description", "Source"],
        [_entry_row(entry, roots) for entry in entries],
    )


def _list(args: argparse.Namespace) -> None:
    """Show every connected account and the agents that hold it.

    The one place "who can read my mail" is answered, for the whole deployment at
    once. A connection nobody holds says so rather than showing an empty column an
    operator has to interpret.
    """
    connections = _connections(args)
    try:
        defined = connections.connections()
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    if not defined:
        _out("No connections on this deployment.")
        _out(f"  bundles are read from: {_roots_line(connections.world.extension_roots)}")
        return
    _print_table(
        ["Connection", "Extension", "Approval", "Granted to"],
        [
            [name, cfg.extension, cfg.approval, ", ".join(cfg.agents) or "(nobody)"]
            for name, cfg in sorted(defined.items())
        ],
    )


def _tools(args: argparse.Namespace) -> None:
    """Show the verbs one instance offers the agent."""
    connections = _connections(args)
    try:
        specs = asyncio.run(connections.tools(args.instance))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    if not specs:
        _out(f"'{args.instance}' offers no tools.")
        return
    _print_table(["Tool", "Classification", "Tags", "Description"], _tool_rows(specs))


def _probe(args: argparse.Namespace) -> None:
    """Prove one instance is live right now."""
    connections = _connections(args)
    try:
        result = asyncio.run(connections.probe(args.instance))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    if not result.reachable:
        _fail(f"probe: '{args.instance}' did not answer — {result.detail}")
    _out(f"'{args.instance}' is reachable. {result.detail}")
    _out(f"  tools: {', '.join(spec.name for spec in result.tools) or '(none served)'}")


def _doctor(args: argparse.Namespace) -> None:
    """Report everything that could be wrong with one instance, without fixing it."""
    connections = _connections(args)
    try:
        checks = asyncio.run(connections.doctor(args.instance))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    _print_table(
        ["Check", "Status", "Detail"],
        [[check.check, check.status, check.detail] for check in checks],
    )


def _approve(args: argparse.Namespace) -> None:
    """Record the tool contract this instance serves right now as approved (REQ-291)."""
    connections = _connections(args)
    try:
        approved = asyncio.run(connections.approve(args.instance))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    _out(f"Approved {len(approved)} tool contract(s) for '{args.instance}':")
    for name in approved:
        _out(f"  {name}")


def _remove(args: argparse.Namespace) -> None:
    """Disconnect one account: its credential, its definition, and every grant on it.

    Never an error when there is nothing to remove — an operator cleaning up
    after a failed install must not be blocked by a step with no work to do.
    """
    connections = _connections(args)
    try:
        report = asyncio.run(connections.remove(args.instance))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    _out(f"Disconnected '{report.instance}'.")
    _out(f"  credentials dropped : {', '.join(report.removed_secrets) or '(none)'}")
    _out(f"  connection removed  : {'yes' if report.removed_config else 'no'}")


# ---------------------------------------------------------------------------
# Terminal presentation
# ---------------------------------------------------------------------------


def _prompt_secrets(plan: arcagent.ConnectorPlan) -> dict[str, str]:
    """Ask for every declared value, hiding the ones that are actually credentials.

    The manifest decides, per field. A hidden prompt is the right protection for a
    token and the wrong one for a base URL: it protects nothing there and
    guarantees that a typo stays invisible until the probe fails with no clue why.
    """
    values: dict[str, str] = {}
    for declared in plan.secrets:
        label = f"{declared.prompt or f'Value for {declared.name}'} [{declared.name}]"
        values[declared.name] = (
            getpass.getpass(f"{label} (hidden): ") if declared.sensitive else input(f"{label}: ")
        )
    return values


def _refuse_unsatisfied_host(plan: arcagent.ConnectorPlan) -> None:
    """Direct the operator to install what is missing. Never install it for them."""
    if not plan.unsatisfied_host:
        return
    err(f"arc connector: host: {plan.extension} needs prerequisites this machine lacks.")
    for verdict in plan.unsatisfied_host:
        err(f"  {verdict.name}: {verdict.instruction}")
    sys.exit(1)


def _tool_rows(specs: Sequence[arcagent.ToolSpec]) -> list[list[str]]:
    return [
        [spec.name, spec.classification, ",".join(spec.capability_tags), spec.description]
        for spec in specs
    ]


def _entry_json(entry: arcagent.CatalogEntry) -> dict[str, Any]:
    """One listing entry, with the full reason when the bundle is unreadable."""
    return {
        "name": entry.name,
        "version": entry.version,
        "description": entry.description,
        "path": str(entry.path),
        "official": entry.official,
        "error": entry.error,
    }


def _entry_row(entry: arcagent.CatalogEntry, roots: Sequence[Path]) -> list[str]:
    """One table row. An unreadable bundle keeps its place and shows its reason."""
    if entry.error:
        return [entry.name, "-", f"unreadable: {_one_line(entry.error)}", _source(entry, roots)]
    return [entry.name, entry.version, entry.description, _source(entry, roots)]


def _source(entry: arcagent.CatalogEntry, roots: Sequence[Path]) -> str:
    """Which root a bundle came from, as its position on the search path."""
    for index, root in enumerate(roots, start=1):
        if entry.path.parent == root:
            return f"{index}: {root}"
    return str(entry.path.parent)


def _one_line(reason: str, limit: int = 90) -> str:
    """A parser's multi-line complaint, shortened to fit a row without lying."""
    collapsed = " ".join(reason.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"


def _roots_line(roots: Sequence[Path]) -> str:
    return ", ".join(str(root) for root in roots) or "(no bundle directory exists)"


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    """The flags every verb shares: where this deployment's connections live."""
    parser.add_argument(
        "--extensions-root",
        default=None,
        help="Use exactly this bundle root (default: the deployment search path).",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Owner-only credential store (default: <arc-dir>/connections.env).",
    )
    parser.add_argument("--arc-dir", default=None, help="Arc config dir (default: ~/.arc).")
    parser.add_argument("--data-dir", default=None, help="Operational data dir for audit/state.")


def _add_agents(parser: argparse.ArgumentParser, *, required: bool) -> None:
    """``--agents a,b`` — who may use this connection. The whole of access control."""
    parser.add_argument(
        "--agents",
        required=required,
        default="",
        help="Comma-separated agent names granted this connection.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc connector",
        description=(
            "Connect this deployment to an external system — available, add, grant, "
            "revoke, auth, authorize, host-setup, list, tools, probe, doctor, "
            "approve, remove."
        ),
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p = subs.add_parser("available", help="List the bundles this deployment can connect.")
    p.add_argument(
        "--extensions-root",
        default=None,
        help="Use exactly this bundle root (default: the deployment search path).",
    )
    p.add_argument("--arc-dir", default=None, help="Arc config dir (default: ~/.arc).")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    p = subs.add_parser("add", help="Connect an account: prompt, probe, persist, grant.")
    p.add_argument("extension", help="Extension bundle name.")
    p.add_argument("--name", required=True, help="Name for this connected account.")
    _add_agents(p, required=False)
    _add_common(p)

    p = subs.add_parser("grant", help="Let more agents use a connected account.")
    p.add_argument("instance", help="Connection name.")
    _add_agents(p, required=True)
    _add_common(p)

    p = subs.add_parser("revoke", help="Take a connected account back from agents.")
    p.add_argument("instance", help="Connection name.")
    _add_agents(p, required=True)
    _add_common(p)

    p = subs.add_parser(
        "auth", help="Authorise an instance: hidden prompt, or the host command to run."
    )
    p.add_argument("instance", help="Connection name.")
    _add_common(p)

    p = subs.add_parser(
        "authorize", help="Sign in a connector whose own binary holds the credential."
    )
    p.add_argument("instance", help="Connection name.")
    _add_common(p)

    p = subs.add_parser(
        "host-setup", help="Install the host binaries a bundle pins, digest-verified."
    )
    p.add_argument("extension", help="Extension bundle name.")
    _add_common(p)

    p = subs.add_parser("list", help="List every connected account and who holds it.")
    _add_common(p)

    p = subs.add_parser("tools", help="Show the verbs an instance offers.")
    p.add_argument("instance", help="Connection name.")
    _add_common(p)

    p = subs.add_parser("probe", help="Prove an instance is reachable right now.")
    p.add_argument("instance", help="Connection name.")
    _add_common(p)

    p = subs.add_parser("doctor", help="Report prerequisites, credentials, and reachability.")
    p.add_argument("instance", help="Connection name.")
    _add_common(p)

    p = subs.add_parser("approve", help="Approve the tool contract an instance serves now.")
    p.add_argument("instance", help="Connection name.")
    _add_common(p)

    p = subs.add_parser("remove", help="Disconnect an account, its credential, its grants.")
    p.add_argument("instance", help="Connection name.")
    _add_common(p)

    return parser


_SUBCOMMAND_MAP = {
    "available": _available,
    "add": _add,
    "grant": _grant,
    "revoke": _revoke,
    "auth": _auth,
    "authorize": _authorize,
    "host-setup": _host_setup,
    "list": _list,
    "tools": _tools,
    "probe": _probe,
    "doctor": _doctor,
    "approve": _approve,
    "remove": _remove,
}


def connector_handler(args: list[str]) -> None:
    """Top-level handler for ``arc connector <sub> [args]``."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)


__all__ = ["connector_handler"]
