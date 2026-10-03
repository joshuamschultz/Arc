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

Paths are explicit and overridable — ``--extensions-root``, ``--arc-dir``,
``--data-dir`` — because an operator running more than one
deployment on a box needs to point a command at one without disturbing another.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

import arcagent
from arctrust import causal
from arctrust.paths import arc_team

from arccli.commands import connector_mcp
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


def _connector_control() -> arcagent.ConnectorControl | None:
    """Optional same-process agent controller; a normal CLI has none."""
    return None


def _connections(args: argparse.Namespace) -> arcagent.Connections:
    """Bind this deployment to the operator-signed chain, or exit naming what is wrong.

    The chain is described, never held: every verb opens and closes its own through
    :class:`~arcagent.connections.AuditChain`, because a ``WormSink`` keeps an
    exclusive ``flock`` for its lifetime and one left open across a ``getpass``
    prompt would lock every later writer out.

    The chain lives with the operational data (the one ``arc task`` and ``arc
    workflow`` write to); the key that signs it lives in the state dir. Both are
    ``--`` overridable so an operator can point one command at one deployment's
    world without touching another's.
    """
    from arccli.commands.operator import operator_worm_sink

    try:
        world = arcagent.resolve_deployment(
            arc_dir=getattr(args, "arc_dir", None),
            data_dir=getattr(args, "data_dir", None),
            extensions_root=getattr(args, "extensions_root", None),
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
        connector_control=_connector_control(),
        oauth_redirect_uri=_oauth_redirect_uri(),
    )


def _oauth_redirect_uri() -> str:
    """The deployment's OAuth redirect URI, from ``[ui] public_base_url`` — config only.

    The same rule ArcUI applies at startup, so a sign-in begun here registers the
    same address. Without arcgateway or a configured origin, the loopback default.
    """
    try:
        from arcgateway.config import GatewayConfig

        base = GatewayConfig.load().ui.public_base_url
    except Exception:  # reason: no gateway config is the ordinary personal default
        base = None
    try:
        return arcagent.oauth_redirect_uri(base)
    except ValueError as exc:
        _fail(str(exc))


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
    _out(f"  credentials in : {world.credential_location}")
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
        mutation = asyncio.run(connections.grant_and_reconcile(args.instance, agents))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    connection = mutation.connection
    if connection is None:
        _fail("connector grant did not produce a connection")
    granted = ", ".join(connection.agents) or "(nobody)"
    _out(f"'{args.instance}' is now granted to: {granted}")
    _print_activations(mutation.activations)


def _revoke(args: argparse.Namespace) -> None:
    """Take one connected account back from agents. The account itself is untouched."""
    connections = _connections(args)
    try:
        mutation = asyncio.run(connections.revoke_and_reconcile(args.instance, _agents(args)))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    connection = mutation.connection
    if connection is None:
        _fail("connector revoke did not produce a connection")
    granted = ", ".join(connection.agents) or "(nobody)"
    _out(f"'{args.instance}' is now granted to: {granted}")
    _print_activations(mutation.activations)


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
    where = connections.world.credential_location
    _out(f"Updated {len(updated)} credential(s) for '{args.instance}' in {where}.")
    _out("  Every agent granted this connection uses the new value on its next call.")


#: How each sign-in state reads in a terminal. "not known" is its own line rather
#: than a silence, because the terminal must not imply either answer.
_SIGN_IN_LINE = {
    "signed_in": "SIGNED IN",
    "signed_out": "NOT signed in",
    "expired": "RECONNECT NEEDED (the stored sign-in stopped working)",
    "not_installed": "NOT installed on this host",
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
        if auth.oauth:
            _authorize_oauth(connections, args.instance, auth)
            return
        if auth.token_binary:
            token = getpass.getpass(f"Token for {auth.token_binary} (hidden): ")
            auth = asyncio.run(connections.authorize(args.instance, token=token))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)

    _direct_host_authorization(auth)
    if not auth.working:
        sys.exit(1)


def _authorize_oauth(connections: Any, instance: str, auth: arcagent.Authorization) -> None:
    """One-click connect from a terminal: open the link, paste where the browser landed.

    Headless parity with the card. Begin and complete run in ONE event loop and one
    process, so the pending sign-in (state, PKCE verifier) never leaves memory. The
    operator pastes the address the browser landed on (or, for a provider that
    shows a code, the code). Nothing typed here is echoed back or logged.
    """
    session = secrets.token_urlsafe(16)

    async def connect() -> Any:
        begun = await connections.begin_oauth(instance, session_id=session)
        if begun.redirect_mode == "none":
            _out("Open this link, click Allow, and copy the code it shows:")
        else:
            _out("Open this link, sign in, and allow access. Then copy the address")
            _out("your browser lands on (it may say the page cannot be reached):")
        _out(f"  {begun.authorize_url}")
        pasted = (await asyncio.to_thread(getpass.getpass, "Paste it here (hidden): ")).strip()
        if not pasted:
            _fail("nothing pasted; the sign-in was not completed.")
        if begun.redirect_mode == "none":
            return await connections.complete_oauth(
                session_id=session, state=begun.state, code=pasted
            )
        return await connections.complete_oauth(session_id=session, redirect_url=pasted)

    try:
        record = asyncio.run(connect())
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    if record.status == "healthy":
        _out(f"  Connected: {auth.extension} answered; the sign-in is stored sealed.")
        return
    _out(f"  Connected, but the check says: {record.status} {record.reason_text or ''}".rstrip())
    sys.exit(1)


def _oauth_app(args: argparse.Namespace) -> None:
    """Set a provider's sign-in app up once for this deployment.

    The secret is never argv: a hidden prompt, or one line on stdin with
    ``--client-secret-stdin`` (so a deploy can pipe a freshly minted secret straight
    in). A tenant-bound provider (Microsoft) also takes ``--tenant-id`` and
    ``--cloud``. Prints the redirect URI to register with the provider.
    """
    connections = _connections(args)
    _out(f"Redirect URI to register with {args.provider}: {connections.oauth_redirect_uri}")
    client_id = (args.client_id or input("Client ID: ")).strip()
    if args.client_secret_stdin:
        client_secret = sys.stdin.readline().strip()
    else:
        client_secret = getpass.getpass("Client secret (hidden): ").strip()
    try:
        asyncio.run(
            connections.set_oauth_app(
                args.provider,
                client_id=client_id,
                client_secret=client_secret,
                tenant_id=args.tenant_id or "",
                cloud=args.cloud or "",
            )
        )
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    _out(
        f"  {args.provider} sign-in is set up. "
        "Connect each account with: arc connector authorize <name>"
    )


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
    records = _health_records(connections)
    _print_table(
        ["Connection", "Extension", "Approval", "Granted to", "Status", "Reason", "Checked"],
        [
            [
                name,
                cfg.extension,
                cfg.approval,
                ", ".join(cfg.agents) or "(nobody)",
                *_health_cells(records.get(name)),
            ]
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


def _semantic(args: argparse.Namespace) -> None:
    """Find, show or check the editable meanings of a connected datastore.

    Introspection can say a table is ``inv_hdr`` with a column ``amt``. Only a
    person can say that is an invoice in dollars, and this is where they say it —
    so the first thing an operator needs is the path, which is why ``--path``
    prints it bare and nothing else: ``$EDITOR "$(arc connector semantic shop
    --path)"``.
    """
    from arcmemory.semantic_layer import (
        SIGNATURE_SUFFIX,
        SemanticLayerTamperedError,
        layer_path,
        load_semantic_layer,
    )

    path = layer_path(args.instance, getattr(args, "arc_dir", None))
    if path is None:
        _fail(f"{args.instance!r} is not a usable connection name")
    if args.path:
        _out(str(path))
        return
    if not path.exists():
        _out(f"No semantic layer yet for '{args.instance}'.")
        _out(f"  expected at : {path}")
        _out("  It is written the first time the datastore's tables are read —")
        _out("  Run `arc knowledge sources` and `arc knowledge resources`/`select`/`map`")
        _out("  for this connection, then run this again.")
        return
    try:
        layer = load_semantic_layer(path)
    except SemanticLayerTamperedError as exc:
        _fail(str(exc))
    if path.with_name(path.name + SIGNATURE_SUFFIX).is_file():
        _out(f"Signed through arcui — a hand edit here will fail verification. {path}")
        _out("")
    if not layer.table:
        _fail(
            f"{path} could not be read as a semantic layer. It is hand-edited, so "
            "this is usually a stray quote or bracket. Agents fall back to the "
            "database's own table names until it parses."
        )
    _out(f"Semantic layer for '{args.instance}': {path}")
    _out("")
    rows = [
        [
            name,
            meaning.entity or "(implied)",
            "hidden" if meaning.hidden else "",
            str(sum(1 for c in meaning.column.values() if c.description)),
            meaning.description or "(no description — agents see only the name)",
        ]
        for name, meaning in sorted(layer.table.items())
    ]
    _print_table(["Table", "One row is", "", "Cols described", "Description"], rows)
    _out("")
    _out(f"Edit it: $EDITOR {path}")


def _health_records(connections: arcagent.Connections) -> dict[str, arcagent.ConnectionRecord]:
    """The stored health records, or nothing when the data plane cannot be read.

    A listing must still answer "who holds what" when the store is down; the status
    columns then read "?" rather than the command failing.
    """
    try:
        return asyncio.run(connections.health_records())
    except Exception:  # reason: the listing's job is grants; health is best-effort here
        return {}


def _health_cells(record: arcagent.ConnectionRecord | None) -> list[str]:
    """STATUS, REASON and CHECKED for one connection, from its stored record."""
    if record is None:
        return ["?", "", "never"]
    return [record.status, record.reason_text or "", record.last_checked_at or "never"]


def _probe(args: argparse.Namespace) -> None:
    """Check one instance right now and record what was found.

    The same check the probe loop runs, so the answer is the connection's health
    record: what this prints is what the card and the next notice will say.
    """
    connections = _connections(args)
    try:
        record = asyncio.run(
            connections.check_health(
                args.instance, checked_by=causal.actor_did(), source="operator"
            )
        )
        tools = asyncio.run(connections.tools(args.instance)) if record.status == "healthy" else ()
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    if record.status != "healthy":
        _fail(
            f"probe: '{args.instance}' is {record.status} — {record.reason_text or 'not checked'}"
        )
    _out(f"'{args.instance}' is healthy.")
    _out(f"  tools: {', '.join(spec.name for spec in tools) or '(none served)'}")


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


def _add_mcp(args: argparse.Namespace) -> None:
    """Add an MCP server: discover, choose tools, generate, sign, install, grant."""
    connections = _connections(args)
    agents = _agents(args)
    _require_deployment_agents(connections, agents)
    connector_mcp.add_mcp(args, connections, agents, _fail)


def _sign(args: argparse.Namespace) -> None:
    """Sign a hand-written bundle with the operator key."""
    connector_mcp.sign(args, _connections(args), _fail)


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
        mutation = asyncio.run(connections.remove_and_reconcile(args.instance))
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    report = mutation.removal
    if report is None:
        _fail("connector removal did not produce a report")
    _out(f"Disconnected '{report.instance}'.")
    _out(f"  credentials dropped : {', '.join(report.removed_secrets) or '(none)'}")
    _out(f"  connection removed  : {'yes' if report.removed_config else 'no'}")
    _print_activations(mutation.activations)


def _print_activations(results: Sequence[arcagent.ConnectorReconcileResult]) -> None:
    """Report immediate activation only when this process actually applied it."""
    for result in results:
        if result.status == "applied":
            _out(
                f"  live activation   : {result.agent}: applied "
                f"({', '.join(result.tools) or 'no connector tools'})"
            )
        else:
            _out(f"  live activation   : {result.agent}: pending (durably queued for its owner)")


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


def _install_bundle(args: argparse.Namespace) -> None:
    """Verify a signed bundle and install it where code may execute from."""
    connections = _connections(args)
    try:
        target = connections.install_bundle(Path(args.bundle), replace=args.replace)
    except arcagent.ExtensionError as exc:
        _fail(exc.message)
    _out(f"Installed {target.name} at {target} (signature verified; verified again at load).")


def _migrate_secrets(args: argparse.Namespace) -> None:
    """Move the legacy credential file into sealed custody: verify, delete, audit."""
    connections = _connections(args)
    if args.reseal:
        _reseal_secrets(connections, dry_run=args.dry_run)
        return
    try:
        report = asyncio.run(
            connections.migrate_secrets(dry_run=args.dry_run, drop_undeclared=args.drop_undeclared)
        )
    except arcagent.ExtensionError as exc:
        _fail(f"{exc.message} (the legacy file was kept)")
    if report.skipped:
        _out(f"Nothing to migrate: {report.path} does not exist.")
        return
    _print_migration(report, where=connections.world.credential_location)


#: What an unresolved reason means, for the migrate-secrets report.
_UNRESOLVED_WHY = {
    "undeclared": "declared by no connection",
    "custody_differs": "custody already holds a different value",
    "app_slot_differs": "the sign-in app slot already holds a different app",
    "app_pair_incomplete": "half of a sign-in app pair",
    "app_values_disagree": "two connections name different apps",
}


def _print_migration(report: Any, *, where: str) -> None:
    """Every key in the legacy file, and what happened (or would happen) to it."""
    verb = "Would move" if report.dry_run else "Moved"
    _out(f"{verb} {len(report.migrated)} credential(s) into {where}.")
    for name in report.migrated:
        _out(f"  moved   : {name}")
    for name in report.already:
        _out(f"  already : {name}  (custody holds the same value)")
    for provider in report.apps:
        _out(f"  app     : {provider} sign-in app slot")
    for key in report.app_keys:
        _out(f"  app key : {key}")
    for key, reason in report.unresolved:
        _out(f"  UNRESOLVED: {key}  ({_UNRESOLVED_WHY.get(reason, reason)})")
    for key in report.dropped:
        _out(f"  dropped : {key}  (on purpose, audited)")
    for key in report.empty:
        _out(f"  empty   : {key}  (no value)")
    if report.dry_run:
        if report.unresolved:
            _out(
                "UNRESOLVED values would be lost. Re-enter any you still need, then run "
                "`arc connector migrate-secrets --drop-undeclared` to drop them on purpose."
            )
        _out(f"Nothing was written. {report.path} is unchanged.")
    elif report.deleted:
        _out(f"Deleted {report.path}.")


def _reseal_secrets(connections: Any, *, dry_run: bool) -> None:
    """Move in-process-sealed credentials under the vault (P18-2F). Safe to re-run."""
    try:
        report = asyncio.run(connections.reseal_secrets(dry_run=dry_run))
    except arcagent.ExtensionError as exc:
        _fail(f"{exc.message} (rows already moved stay moved; re-run to continue)")
    if report.dry_run:
        _out(f"Would re-seal {len(report.pending)} connection(s) under the vault key.")
        for name in report.pending:
            _out(f"  pending : {name}")
        for provider in report.apps:
            _out(f"  pending : {provider} sign-in app")
        _out("Nothing was written.")
        return
    _out(f"Re-sealed {len(report.resealed)} connection(s) under the vault key.")
    for name in report.resealed:
        _out(f"  resealed: {name}")
    for name in report.already:
        _out(f"  already : {name}")
    for provider in report.apps:
        _out(f"  resealed: {provider} sign-in app")


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
    parser.add_argument("--arc-dir", default=None, help="Operator root (default: ~/arc).")
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
            "Connect this deployment to an external system — available, add, add-mcp, "
            "sign, grant, revoke, auth, authorize, host-setup, list, tools, probe, "
            "doctor, approve, remove, migrate-secrets."
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
    p.add_argument("--arc-dir", default=None, help="Operator root (default: ~/arc).")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    p = subs.add_parser("add", help="Connect an account: prompt, probe, persist, grant.")
    p.add_argument("extension", help="Extension bundle name.")
    p.add_argument("--name", required=True, help="Name for this connected account.")
    _add_agents(p, required=False)
    _add_common(p)

    p = subs.add_parser(
        "add-mcp", help="Add your own MCP server: discover tools, sign a bundle, connect, grant."
    )
    connector_mcp.add_mcp_arguments(p)

    p = subs.add_parser("sign", help="Sign a hand-written bundle with the operator key.")
    connector_mcp.add_sign_arguments(p)

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
        "oauth-app", help="Set a provider's sign-in app up once (client ID and secret)."
    )
    p.add_argument(
        "provider", help="Provider app slot, as the bundle's [oauth] provider names it."
    )
    p.add_argument("--client-id", default="", help="The app's client ID (else prompted).")
    p.add_argument("--tenant-id", default="", help="Directory (tenant) ID GUID, for Microsoft.")
    p.add_argument(
        "--cloud", default="", help="Cloud key for Microsoft: global (default), usgov or dod."
    )
    p.add_argument(
        "--client-secret-stdin",
        action="store_true",
        help="Read the client secret as one line from stdin instead of a hidden prompt.",
    )
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

    p = subs.add_parser(
        "semantic",
        help="Show or locate the editable meanings of a connected datastore's tables.",
    )
    p.add_argument("instance", help="Connection name.")
    p.add_argument(
        "--path",
        action="store_true",
        help="Print only the file path, for piping into an editor.",
    )
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

    p = subs.add_parser(
        "install-bundle",
        help="Install a signed connector bundle that carries code into ~/.arc/extensions.",
    )
    p.add_argument("bundle", help="Path to a signed bundle folder holding extension.toml.")
    p.add_argument("--replace", action="store_true", help="Replace an installed bundle.")
    _add_common(p)

    p = subs.add_parser(
        "migrate-secrets",
        help="Move the legacy plaintext connector credential file into sealed custody.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show exactly what would move into custody, map to a sign-in app slot, "
            "or be lost. Writes nothing."
        ),
    )
    p.add_argument(
        "--drop-undeclared",
        action="store_true",
        help=(
            "Drop, on purpose, the values no connection declares (each audited by name). "
            "Without it any such value stops the migration and the file is kept."
        ),
    )
    p.add_argument(
        "--reseal",
        action="store_true",
        help=(
            "After switching to custody = vault_transit: re-seal credentials sealed under "
            "the old in-process operator key into the vault. Crash-safe and safe to re-run."
        ),
    )
    _add_common(p)

    return parser


_SUBCOMMAND_MAP = {
    "available": _available,
    "add": _add,
    "add-mcp": _add_mcp,
    "sign": _sign,
    "grant": _grant,
    "revoke": _revoke,
    "auth": _auth,
    "authorize": _authorize,
    "oauth-app": _oauth_app,
    "host-setup": _host_setup,
    "list": _list,
    "tools": _tools,
    "probe": _probe,
    "semantic": _semantic,
    "doctor": _doctor,
    "approve": _approve,
    "remove": _remove,
    "migrate-secrets": _migrate_secrets,
    "install-bundle": _install_bundle,
}


def connector_handler(args: list[str]) -> None:
    """Top-level handler for ``arc connector <sub> [args]``.

    Item 20: every credential read or change this command causes is attributed
    to the person at the terminal; the operator key that signs the audit chain
    is recorded separately as its signer.
    """
    with causal.bind(causal.root("operator", _cli_actor())):
        dispatch(_build_parser(), _SUBCOMMAND_MAP, connector_mcp.rewrite_command(args))


def _cli_actor() -> str:
    """The OS account running ``arc``, as a pseudo-DID."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError):  # reason: no passwd entry / login name in a container
        user = "unknown"
    return f"did:arc:cli:{user}"


__all__ = ["connector_handler"]
