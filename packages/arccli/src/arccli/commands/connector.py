"""``arc connector`` — connect an agent to an external system, and manage it.

SPEC-062 COMP-016. Eight verbs make up the COMPLETE management surface (REQ-293):
``add``, ``auth``, ``list``, ``tools``, ``probe``, ``doctor``, ``approve``, and
``remove``. The terminal is sufficient for all of them; the web panel is a
convenience that is required for nothing (D-561).

This module owns exactly one thing the shared install path cannot: **asking the
operator**. Every declared credential is collected with ``getpass``, which does
not echo, and handed straight to :mod:`arcagent.modules.connectors.install`,
which writes it to the per-agent secret store and nowhere else — never into a
config file, a log, a prompt, or model context. There is deliberately no
``--token``-style flag: a credential on argv lands in shell history and in the
process table, which is the exact exposure the hidden prompt exists to remove.
This is the constraint ``gateway_connect.py`` states in its own docstring, and
it is why connector setup is a CLI/settings action rather than something an
agent could be asked to do in chat (LLM07).

The ordering, the rollback, and the config write live in the install module so
arctui and arcui drive the same code rather than re-deriving the sequence.

Paths are explicit and overridable — ``--extensions-root``, ``--env-file``,
``--arc-dir``, ``--data-dir`` — because an operator running a fleet needs to
point a command at one agent's world without disturbing another's.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ExtensionAttachment, ToolSpec
from arcagent.extension.secrets import SecretRef, SecretStore, select_secret_backend
from arcagent.modules.connectors.install import (
    AttachmentFactory,
    ConnectorPlan,
    InstanceConfig,
    build_attachment,
    install_connector,
    load_egress_allow,
    load_instances,
    plan_connector,
    remove_connector,
)

from arccli.commands._shared import dispatch, err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

#: Where an agent's bundles live unless the operator points elsewhere.
_BUNDLES_DIRNAME = "extensions"

#: The agent's own owner-only credential file (D-555) unless overridden.
_ENV_FILENAME = "connectors.env"


@dataclass(frozen=True)
class _Context:
    """Everything every verb needs, resolved once from the agent directory."""

    agent_dir: Path
    arc_dir: Path
    data_dir: Path
    agent: str
    did: str
    tier: Tier
    extensions_root: Path
    env_file: Path


# ---------------------------------------------------------------------------
# Seams — module-level so a test can substitute them without patching internals
# ---------------------------------------------------------------------------


def _attachment_factory() -> AttachmentFactory:
    """How a manifest becomes something probeable."""
    return build_attachment


def _audit_sink(arc_dir: Path, data_dir: Path) -> Any:
    """The operator-signed WORM sink every connector verdict lands in.

    The chain lives with the operational data (the same one ``arc task`` and
    ``arc workflow`` write to); the key that signs it lives in the config dir.
    Both are ``--`` overridable so an operator can point one command at one
    deployment's world without touching another's.

    The caller MUST close it: the sink holds an exclusive ``flock`` for its
    lifetime, so an unclosed one locks every later writer out of the chain.
    """
    from arcstore.ingest import WORM_ACTIVE_FILENAME
    from arctrust import WormSink

    from arccli.commands.operator import resolve_operator_signer, resolve_record_cipher

    worm_dir = data_dir / "worm"
    worm_dir.mkdir(parents=True, exist_ok=True)
    return WormSink(
        worm_dir / WORM_ACTIVE_FILENAME,
        resolve_operator_signer(arc_dir),
        cipher=resolve_record_cipher(arc_dir),
    )


@contextlib.contextmanager
def _audited(ctx: _Context) -> Iterator[Any]:
    """Open the audit chain for one command, and always close it."""
    sink = _audit_sink(ctx.arc_dir, ctx.data_dir)
    try:
        yield sink
    finally:
        sink.close()


# ---------------------------------------------------------------------------
# Context + shared helpers
# ---------------------------------------------------------------------------


def _context(args: argparse.Namespace) -> _Context:
    """Resolve the agent's identity, tier, and paths, or exit naming what is wrong."""
    agent_dir = Path(args.agent).expanduser().resolve()
    config = agent_dir / "arcagent.toml"
    if not config.is_file():
        _fail(f"no arcagent.toml at {config} — is that an agent directory?")

    try:
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _fail(f"{config} — {exc}")

    did = str(raw.get("identity", {}).get("did", ""))
    if not did:
        _fail(f"{config} has no [identity].did — run 'arc agent build' first.")

    arc_dir = Path(getattr(args, "arc_dir", None) or Path.home() / ".arc").expanduser()
    return _Context(
        agent_dir=agent_dir,
        arc_dir=arc_dir,
        data_dir=_data_dir(args),
        agent=agent_dir.name,
        did=did,
        tier=Tier(str(raw.get("security", {}).get("tier", "personal"))),
        extensions_root=_path_or(
            getattr(args, "extensions_root", None), agent_dir / _BUNDLES_DIRNAME
        ),
        env_file=_path_or(getattr(args, "env_file", None), agent_dir / _ENV_FILENAME),
    )


def _data_dir(args: argparse.Namespace) -> Path:
    """The operational data directory — arcstore's, unless the operator names one."""
    given = getattr(args, "data_dir", None)
    if given:
        path = Path(given).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    from arcstore import resolve_data_dir

    return Path(resolve_data_dir(None))


def _pinned_key(ctx: _Context) -> bytes | None:
    """The operator key an extension bundle's signatures are pinned to (REQ-283).

    Read-only: never mints a key, so an install above personal tier on a machine
    with no operator key is refused by the loader rather than quietly satisfied by
    a keypair this command generated moments earlier.
    """
    from arccli.commands.operator import operator_public_key

    return operator_public_key(ctx.arc_dir)


def _path_or(given: str | None, fallback: Path) -> Path:
    return Path(given).expanduser().resolve() if given else fallback


def _store(ctx: _Context, sink: Any) -> SecretStore:
    """The one place a connector credential is written or read."""
    try:
        backend = select_secret_backend(ctx.tier, env_file=ctx.env_file)
    except ExtensionError as exc:
        _fail(exc.message)
    return SecretStore(backend, sink=sink)


def _plan(ctx: _Context, extension: str, instance: str, sink: Any) -> ConnectorPlan:
    """Resolve and parse a bundle, or exit naming the step that refused it."""
    try:
        return plan_connector(
            extensions_root=ctx.extensions_root,
            extension=extension,
            instance=instance,
            tier=ctx.tier,
            audit_sink=sink,
            egress_allow=load_egress_allow(ctx.agent_dir),
        )
    except ExtensionError as exc:
        _fail(exc.message)


def _installed(ctx: _Context) -> dict[str, InstanceConfig]:
    try:
        return load_instances(ctx.agent_dir)
    except ExtensionError as exc:
        _fail(exc.message)


def _plan_for_instance(ctx: _Context, instance: str, sink: Any) -> ConnectorPlan:
    """The plan behind an already-configured instance."""
    configured = _installed(ctx).get(instance)
    if configured is None:
        _fail(
            f"no connector instance named {instance!r} on {ctx.agent} — try 'arc connector list'"
        )
    return _plan(ctx, configured.extension, instance, sink)


def _attachment(plan: ConnectorPlan) -> ExtensionAttachment:
    try:
        return _attachment_factory()(plan.manifest, plan.bundle)
    except ExtensionError as exc:
        _fail(exc.message)
    except Exception as exc:  # reason: an unbuildable attachment is an operator error
        _fail(f"{plan.extension}: {type(exc).__name__}: {exc}")


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


def _fail(message: str) -> NoReturn:
    """Report the problem to stderr and stop. Never returns."""
    err(f"arc connector: {message}")
    sys.exit(1)


def _tool_rows(specs: Sequence[ToolSpec]) -> list[list[str]]:
    return [
        [spec.name, spec.classification, ",".join(spec.capability_tags), spec.description]
        for spec in specs
    ]


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _add(args: argparse.Namespace) -> None:
    """Install one connected account: prompt, probe, and persist only on success."""
    ctx = _context(args)
    with _audited(ctx) as sink:
        plan = _plan(ctx, args.extension, args.instance, sink)
        _refuse_unsatisfied_host(plan)
        values = _prompt_secrets(plan)
        try:
            report = asyncio.run(
                install_connector(
                    plan,
                    agent_dir=ctx.agent_dir,
                    agent=ctx.agent,
                    secret_values=values,
                    store=_store(ctx, sink),
                    caller_did=ctx.did,
                    attachment_factory=_attachment_factory(),
                    audit_sink=sink,
                    trusted_public_key=_pinned_key(ctx),
                )
            )
        except ExtensionError as exc:
            _fail(exc.message)

    _out(f"Connected {report.extension} as instance '{report.instance}'.")
    _out(f"  agent          : {ctx.agent}")
    _out(f"  approval mode  : {plan.approval_mode}")
    _out(f"  credentials in : {ctx.env_file}  (owner-only)")
    if report.detail:
        _out(f"  probe          : {report.detail}")
    _out(f"  tools          : {', '.join(report.tools) or '(none served)'}")


def _auth(args: argparse.Namespace) -> None:
    """Re-supply this instance's credentials — a rotation, or a first-time fix."""
    ctx = _context(args)
    with _audited(ctx) as sink:
        plan = _plan_for_instance(ctx, args.instance, sink)
        if not plan.secrets:
            _out(f"{plan.extension} declares no credentials; nothing to supply.")
            return
        store = _store(ctx, sink)
        values = _prompt_secrets(plan)
        try:
            asyncio.run(_put_secrets(store, ctx, args.instance, values))
        except ExtensionError as exc:
            _fail(exc.message)
    _out(f"Updated {len(values)} credential(s) for '{args.instance}' in {ctx.env_file}.")


async def _put_secrets(
    store: SecretStore, ctx: _Context, instance: str, values: Mapping[str, str]
) -> None:
    for field, value in values.items():
        ref = SecretRef(agent=ctx.agent, instance=instance, field=field)
        await store.put(ref, value, caller_did=ctx.did)


def _list(args: argparse.Namespace) -> None:
    """Show every connected account this agent has."""
    ctx = _context(args)
    instances = _installed(ctx)
    if not instances:
        _out(f"No connector instances configured for {ctx.agent}.")
        _out(f"  bundles are read from: {ctx.extensions_root}")
        return
    _print_table(
        ["Instance", "Extension", "Approval"],
        [[name, cfg.extension, cfg.approval] for name, cfg in sorted(instances.items())],
    )


def _tools(args: argparse.Namespace) -> None:
    """Show the verbs one instance offers the agent."""
    ctx = _context(args)
    with _audited(ctx) as sink:
        plan = _plan_for_instance(ctx, args.instance, sink)
        specs = asyncio.run(_attachment(plan).describe_tools())
    if not specs:
        _out(f"'{args.instance}' offers no tools.")
        return
    _print_table(["Tool", "Classification", "Tags", "Description"], _tool_rows(specs))


def _probe(args: argparse.Namespace) -> None:
    """Prove one instance is live right now."""
    ctx = _context(args)
    with _audited(ctx) as sink:
        plan = _plan_for_instance(ctx, args.instance, sink)
        result = asyncio.run(_attachment(plan).probe())
    if not result.reachable:
        _fail(f"probe: '{args.instance}' did not answer — {result.detail}")
    _out(f"'{args.instance}' is reachable. {result.detail}")
    _out(f"  tools: {', '.join(spec.name for spec in result.tools) or '(none served)'}")


def _doctor(args: argparse.Namespace) -> None:
    """Report everything that could be wrong with one instance, without fixing it."""
    ctx = _context(args)
    with _audited(ctx) as sink:
        plan = _plan_for_instance(ctx, args.instance, sink)
        store = _store(ctx, sink)
        rows = [
            [verdict.name, "missing", verdict.instruction] for verdict in plan.unsatisfied_host
        ]
        rows += asyncio.run(_credential_rows(store, ctx, args.instance, plan))
        rows.append(_reachability_row(plan))
    _print_table(["Check", "Status", "Detail"], rows)


async def _credential_rows(
    store: SecretStore, ctx: _Context, instance: str, plan: ConnectorPlan
) -> list[list[str]]:
    """One row per declared credential: present or missing, never the value."""
    rows: list[list[str]] = []
    for declared in plan.secrets:
        ref = SecretRef(agent=ctx.agent, instance=instance, field=declared.name)
        found = await store.get(ref, caller_did=ctx.did)
        rows.append([declared.name, "present" if found else "missing", str(ctx.env_file)])
    return rows


def _reachability_row(plan: ConnectorPlan) -> list[str]:
    """Probing is the only honest answer to "does this connection work"."""
    try:
        result = asyncio.run(_attachment_factory()(plan.manifest, plan.bundle).probe())
    except Exception as exc:  # reason: doctor reports failures, it does not raise them
        return ["connection", "error", f"{type(exc).__name__}: {exc}"]
    return ["connection", "reachable" if result.reachable else "unreachable", result.detail]


def _approve(args: argparse.Namespace) -> None:
    """Record the tool contract this instance serves right now as approved (REQ-291)."""
    ctx = _context(args)
    with _audited(ctx) as sink:
        plan = _plan_for_instance(ctx, args.instance, sink)
        specs = asyncio.run(_attachment(plan).describe_tools())
        asyncio.run(_record_approval(ctx, args.instance, specs, sink))
    _out(f"Approved {len(specs)} tool contract(s) for '{args.instance}':")
    for spec in specs:
        _out(f"  {spec.name}")


async def _record_approval(
    ctx: _Context, instance: str, specs: Sequence[ToolSpec], sink: Any
) -> None:
    from arcagent.extension.contract_ledger import ToolContractLedger
    from arcagent.extension.state import open_connection_state

    state = await open_connection_state(str(ctx.data_dir))
    ledger = ToolContractLedger(state, agent=ctx.agent, instance=instance, sink=sink)
    await ledger.approve(specs, actor_did=ctx.did)


def _remove(args: argparse.Namespace) -> None:
    """Drop one connected account: its credentials, its config block, its state.

    Never an error when there is nothing to remove — an operator cleaning up
    after a failed install must not be blocked by a step with no work to do.
    """
    ctx = _context(args)
    with _audited(ctx) as sink:
        fields = _declared_secret_fields(ctx, args.instance, sink)
        report = asyncio.run(
            remove_connector(
                agent_dir=ctx.agent_dir,
                agent=ctx.agent,
                instance=args.instance,
                store=_store(ctx, sink),
                caller_did=ctx.did,
                secret_fields=fields,
            )
        )
    _out(f"Removed connector instance '{report.instance}'.")
    _out(f"  credentials dropped : {', '.join(report.removed_secrets) or '(none)'}")
    _out(f"  config block removed: {'yes' if report.removed_config else 'no'}")


def _declared_secret_fields(ctx: _Context, instance: str, sink: Any) -> list[str]:
    """Which credentials this instance declared, when the bundle is still readable.

    A bundle deleted before its connection was removed leaves nothing to read, so
    removal proceeds on the config block alone rather than refusing to clean up.
    """
    configured = _installed(ctx).get(instance)
    if configured is None:
        return []
    try:
        plan = plan_connector(
            extensions_root=ctx.extensions_root,
            extension=configured.extension,
            instance=instance,
            tier=ctx.tier,
            audit_sink=sink,
        )
    except ExtensionError:
        return []
    return [declared.name for declared in plan.secrets]


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    """The flags every verb shares: which agent, and where its world lives."""
    parser.add_argument("--agent", required=True, help="Path to the agent directory.")
    parser.add_argument(
        "--extensions-root",
        default=None,
        help=f"Where bundles live (default: <agent>/{_BUNDLES_DIRNAME}).",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help=f"Owner-only credential store (default: <agent>/{_ENV_FILENAME}).",
    )
    parser.add_argument("--arc-dir", default=None, help="Arc config dir (default: ~/.arc).")
    parser.add_argument("--data-dir", default=None, help="Operational data dir for audit/state.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc connector",
        description=(
            "Connect an agent to an external system — add, auth, list, tools, "
            "probe, doctor, approve, remove."
        ),
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p = subs.add_parser("add", help="Install a connection: prompt, probe, then persist.")
    p.add_argument("extension", help="Extension bundle name.")
    p.add_argument("--instance", required=True, help="Name for this connected account.")
    _add_common(p)

    p = subs.add_parser("auth", help="Re-supply an instance's credentials (hidden prompt).")
    p.add_argument("instance", help="Connected account name.")
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
    "add": _add,
    "auth": _auth,
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
