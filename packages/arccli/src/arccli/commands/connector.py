"""``arc connector`` — connect an agent to an external system, and manage it.

SPEC-062 COMP-016, SPEC-064 T-011. Eleven verbs make up the COMPLETE management
surface (REQ-293): ``available``, ``add``, ``auth``, ``authorize``,
``host-setup``, ``list``, ``tools``, ``probe``, ``doctor``, ``approve``, and
``remove``. The terminal is sufficient for all of them; the web panel is a
convenience that is required for nothing (D-561). ``available`` is the one verb
that needs no agent — an operator asking what can be connected has not chosen one
yet.

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
``--arc-dir``, ``--data-dir`` — because an operator running a fleet needs to
point a command at one agent's world without disturbing another's.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from arcagent.connections import (
    AttachmentFactory,
    AuditChain,
    Authorization,
    CatalogEntry,
    Connections,
    ConnectorPlan,
    ExtensionError,
    Tier,
    ToolSpec,
    agent_tier,
    catalog,
    resolve_roots,
    resolve_world,
)

from arccli.commands._shared import dispatch, err
from arccli.commands._shared import print_json as _print_json
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

# ---------------------------------------------------------------------------
# Seams — module-level so a test can substitute them without patching internals
# ---------------------------------------------------------------------------


def _attachment_factory() -> AttachmentFactory | None:
    """How a manifest becomes something probeable. ``None`` means the shipped builder."""
    return None


def _connections(args: argparse.Namespace) -> Connections:
    """Bind one agent's world to the operator-signed chain, or exit naming what is wrong.

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
        world = resolve_world(
            args.agent,
            arc_dir=getattr(args, "arc_dir", None) or Path.home() / ".arc",
            data_dir=getattr(args, "data_dir", None),
            extensions_root=getattr(args, "extensions_root", None),
            env_file=getattr(args, "env_file", None),
        )
    except ExtensionError as exc:
        _fail(exc.message)
    return Connections(
        world,
        audit=AuditChain.opened_by(lambda: operator_worm_sink(world.arc_dir, world.data_dir)),
        attachment_factory=_attachment_factory(),
    )


def _fail(message: str) -> NoReturn:
    """Report the problem to stderr and stop. Never returns."""
    err(f"arc connector: {message}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _add(args: argparse.Namespace) -> None:
    """Install one connected account: prompt, probe, and persist only on success."""
    connections = _connections(args)
    world = connections.world
    try:
        plan = connections.plan(args.extension, args.instance)
        _refuse_unsatisfied_host(plan)
        report = asyncio.run(connections.install(plan, _prompt_secrets(plan)))
    except ExtensionError as exc:
        _fail(exc.message)

    _out(f"Connected {report.extension} as instance '{report.instance}'.")
    _out(f"  agent          : {world.agent}")
    _out(f"  approval mode  : {plan.approval_mode}")
    _out(f"  credentials in : {world.env_file}  (owner-only)")
    if report.detail:
        _out(f"  probe          : {report.detail}")
    _out(f"  tools          : {', '.join(report.tools) or '(none served)'}")


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
    except ExtensionError as exc:
        _fail(exc.message)
    env_file = connections.world.env_file
    _out(f"Updated {len(updated)} credential(s) for '{args.instance}' in {env_file}.")


def _direct_host_authorization(auth: Authorization) -> None:
    """Print the exact command that authorises a connector whose binary owns its token.

    "declares no credentials; nothing to supply" was true and useless: ``gh`` does
    need authorising, just not by Arc, and an operator who reads that has nowhere
    to go. Every line here is something to type or something already done.
    """
    _out(f"{auth.extension} keeps its own credential; Arc never holds one for it.")
    if not auth.hosts:
        _out("  Its manifest names no authorising command — see the bundle's host_requires.")
    for host in auth.hosts:
        _out(f"  Run on this host: {host.command}")
    if auth.token_binary:
        _out(f"  Or let Arc do it: arc connector authorize {auth.instance}")
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
    except ExtensionError as exc:
        _fail(exc.message)

    _direct_host_authorization(auth)
    if not auth.reachable:
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
    except ExtensionError as exc:
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

    Works without ``--agent`` because there is a fleet-wide answer; with one, that
    agent's own bundles are searched first, which is the order every other verb
    resolves in. Nothing is installed, nothing is written, and nothing is
    audited — no verdict is taken here, and the refusal an operator acts on is
    taken (and recorded) by ``add``.
    """
    agent_dir = Path(args.agent).expanduser().resolve() if args.agent else None
    try:
        tier = agent_tier(agent_dir) if agent_dir else Tier.PERSONAL
    except ExtensionError as exc:
        _fail(exc.message)
    roots = resolve_roots(agent_dir, extensions_root=args.extensions_root)
    entries = catalog(roots=roots, tier=tier)

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
    """Show every connected account this agent has."""
    connections = _connections(args)
    try:
        instances = connections.installed()
    except ExtensionError as exc:
        _fail(exc.message)
    if not instances:
        _out(f"No connector instances configured for {connections.world.agent}.")
        _out(f"  bundles are read from: {_roots_line(connections.world.extension_roots)}")
        return
    _print_table(
        ["Instance", "Extension", "Approval"],
        [[name, cfg.extension, cfg.approval] for name, cfg in sorted(instances.items())],
    )


def _tools(args: argparse.Namespace) -> None:
    """Show the verbs one instance offers the agent."""
    connections = _connections(args)
    try:
        specs = asyncio.run(connections.tools(args.instance))
    except ExtensionError as exc:
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
    except ExtensionError as exc:
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
    except ExtensionError as exc:
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
    except ExtensionError as exc:
        _fail(exc.message)
    _out(f"Approved {len(approved)} tool contract(s) for '{args.instance}':")
    for name in approved:
        _out(f"  {name}")


def _remove(args: argparse.Namespace) -> None:
    """Drop one connected account: its credentials, its config block, its state.

    Never an error when there is nothing to remove — an operator cleaning up
    after a failed install must not be blocked by a step with no work to do.
    """
    connections = _connections(args)
    try:
        report = asyncio.run(connections.remove(args.instance))
    except ExtensionError as exc:
        _fail(exc.message)
    _out(f"Removed connector instance '{report.instance}'.")
    _out(f"  credentials dropped : {', '.join(report.removed_secrets) or '(none)'}")
    _out(f"  config block removed: {'yes' if report.removed_config else 'no'}")


# ---------------------------------------------------------------------------
# Terminal presentation
# ---------------------------------------------------------------------------


def _prompt_secrets(plan: ConnectorPlan) -> dict[str, str]:
    """Collect every declared credential with a non-echoing prompt."""
    values: dict[str, str] = {}
    for declared in plan.secrets:
        label = declared.prompt or f"Value for {declared.name}"
        values[declared.name] = getpass.getpass(f"{label} (hidden): ")
    return values


def _refuse_unsatisfied_host(plan: ConnectorPlan) -> None:
    """Direct the operator to install what is missing. Never install it for them."""
    if not plan.unsatisfied_host:
        return
    err(f"arc connector: host: {plan.extension} needs prerequisites this machine lacks.")
    for verdict in plan.unsatisfied_host:
        err(f"  {verdict.name}: {verdict.instruction}")
    sys.exit(1)


def _tool_rows(specs: Sequence[ToolSpec]) -> list[list[str]]:
    return [
        [spec.name, spec.classification, ",".join(spec.capability_tags), spec.description]
        for spec in specs
    ]


def _entry_json(entry: CatalogEntry) -> dict[str, Any]:
    """One listing entry, with the full reason when the bundle is unreadable."""
    return {
        "name": entry.name,
        "version": entry.version,
        "description": entry.description,
        "path": str(entry.path),
        "official": entry.official,
        "error": entry.error,
    }


def _entry_row(entry: CatalogEntry, roots: Sequence[Path]) -> list[str]:
    """One table row. An unreadable bundle keeps its place and shows its reason."""
    if entry.error:
        return [entry.name, "-", f"unreadable: {_one_line(entry.error)}", _source(entry, roots)]
    return [entry.name, entry.version, entry.description, _source(entry, roots)]


def _source(entry: CatalogEntry, roots: Sequence[Path]) -> str:
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
    """The flags every verb shares: which agent, and where its world lives."""
    parser.add_argument("--agent", required=True, help="Path to the agent directory.")
    parser.add_argument(
        "--extensions-root",
        default=None,
        help="Use exactly this bundle root (default: the deployment search path).",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Owner-only credential store (default: <agent>/connectors.env).",
    )
    parser.add_argument("--arc-dir", default=None, help="Arc config dir (default: ~/.arc).")
    parser.add_argument("--data-dir", default=None, help="Operational data dir for audit/state.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc connector",
        description=(
            "Connect an agent to an external system — available, add, auth, "
            "authorize, host-setup, list, tools, probe, doctor, approve, remove."
        ),
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p = subs.add_parser("available", help="List the bundles this deployment can connect.")
    # --agent is optional here and required everywhere else: the fleet-wide search
    # path has an answer before an operator has chosen which agent to connect.
    p.add_argument("--agent", default=None, help="Also search this agent's own bundles.")
    p.add_argument(
        "--extensions-root",
        default=None,
        help="Use exactly this bundle root (default: the deployment search path).",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    p = subs.add_parser("add", help="Install a connection: prompt, probe, then persist.")
    p.add_argument("extension", help="Extension bundle name.")
    p.add_argument("--instance", required=True, help="Name for this connected account.")
    _add_common(p)

    p = subs.add_parser(
        "auth", help="Authorise an instance: hidden prompt, or the host command to run."
    )
    p.add_argument("instance", help="Connected account name.")
    _add_common(p)

    p = subs.add_parser(
        "authorize", help="Sign in a connector whose own binary holds the credential."
    )
    p.add_argument("instance", help="Connected account name.")
    _add_common(p)

    p = subs.add_parser(
        "host-setup", help="Install the host binaries a bundle pins, digest-verified."
    )
    p.add_argument("extension", help="Extension bundle name.")
    _add_common(p)

    p = subs.add_parser("list", help="List this agent's connected accounts.")
    _add_common(p)

    p = subs.add_parser("tools", help="Show the verbs an instance offers.")
    p.add_argument("instance", help="Connected account name.")
    _add_common(p)

    p = subs.add_parser("probe", help="Prove an instance is reachable right now.")
    p.add_argument("instance", help="Connected account name.")
    _add_common(p)

    p = subs.add_parser("doctor", help="Report prerequisites, credentials, and reachability.")
    p.add_argument("instance", help="Connected account name.")
    _add_common(p)

    p = subs.add_parser("approve", help="Approve the tool contract an instance serves now.")
    p.add_argument("instance", help="Connected account name.")
    _add_common(p)

    p = subs.add_parser("remove", help="Remove an instance, its credentials, and its state.")
    p.add_argument("instance", help="Connected account name.")
    _add_common(p)

    return parser


_SUBCOMMAND_MAP = {
    "available": _available,
    "add": _add,
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
