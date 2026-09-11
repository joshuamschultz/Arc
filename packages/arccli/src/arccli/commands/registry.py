"""Centralized slash command registry — arccli.commands.registry.

SDD §3.11: Single source of truth for arccli, arcgateway, and platform adapters.

Every slash command is described by a CommandDef. Handlers are attached lazily
at dispatch time to avoid circular imports and keep this module fast to import.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# CommandDef — frozen descriptor for one slash command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandDef:
    """Descriptor for a single slash command.

    Attributes
    ----------
    name:
        Canonical command name without leading slash (e.g. ``"help"``).
    description:
        Short human-readable description shown in help output.
    category:
        Grouping used by all render helpers.
    aliases:
        Alternative names that resolve to this command. No leading slash.
    args_hint:
        Optional short usage hint shown in help (e.g. ``"<agent-dir>"``).
    cli_only:
        True if this command must not appear in gateway/Telegram/Slack surfaces.
    gateway_only:
        True if this command is only meaningful in a gateway context.
    gateway_config_gate:
        Dotpath in config that must be truthy for this command to be visible
        in gateway help menus (command is always dispatchable when registered).
    handler:
        Callable attached at registration time. Optional; resolved at dispatch.
        Not serialised or compared during frozen equality checks.
    """

    name: str
    description: str
    category: Literal["Session", "Configuration", "Tools & Skills", "Info", "Exit"]
    aliases: tuple[str, ...] = field(default=())
    args_hint: str = ""
    cli_only: bool = False
    gateway_only: bool = False
    gateway_config_gate: str | None = None
    handler: Callable[[list[str]], None] | None = field(default=None, compare=False, hash=False)


# ---------------------------------------------------------------------------
# COMMAND_REGISTRY — authoritative list of all commands
#
# Handlers are imported lazily inside each handle() wrapper so that the
# registry module has zero dependency on handler modules at import time.
# This keeps cold-start fast and avoids circular imports.
# ---------------------------------------------------------------------------


def _agent_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.agent import agent_handler

    agent_handler(args)


def _llm_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.llm import llm_handler

    llm_handler(args)


def _run_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.run import run_handler

    run_handler(args)


def _skill_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.skill import skill_handler

    skill_handler(args)


def _team_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.team import team_handler

    team_handler(args)


def _ui_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.ui import ui_handler

    ui_handler(args)


def _store_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.store import store_handler

    store_handler(args)


def _ext_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.ext import ext_handler

    ext_handler(args)


def _connector_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.connector import connector_handler

    connector_handler(args)


def _keys_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.keys import keys_handler

    keys_handler(args)


def _task_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.task import task_handler

    task_handler(args)


def _memory_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.memory import memory_handler

    memory_handler(args)


def _module_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.module import module_handler

    module_handler(args)


def _install_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.install import install_handler

    install_handler(args)


def _runtime_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.runtime import runtime_handler

    runtime_handler(args)


def _up_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.up import up_handler

    up_handler(args)


def _restart_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.restart import restart_handler

    restart_handler(args)


def _prompt_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.prompt import prompt_handler

    prompt_handler(args)


def _approve_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.approve import approve_handler

    approve_handler(args)


def _stop_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.stop import stop_handler

    stop_handler(args)


def _blueprint_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.blueprint import blueprint_handler

    blueprint_handler(args)


def _user_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.user import user_handler

    user_handler(args)


def _trust_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.trust import trust_handler

    trust_handler(args)


def _capability_import_handler(args: list[str]) -> None:
    """Dispatch staged capability-import review commands."""
    from arccli.commands.capability_import import capability_import_handler

    capability_import_handler(args)


def _workflow_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.workflow import workflow_handler

    workflow_handler(args)


def _identity_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.identity import identity_handler

    identity_handler(args)


def _init_handler(args: list[str]) -> None:
    """Dispatch wrapper."""
    from arccli.commands.init import init_handler

    init_handler(args)


def _help_handler(args: list[str]) -> None:
    """Print help text — rendered from registry at call time."""
    from arccli.commands.render import commands_by_category

    by_cat = commands_by_category()
    sys.stdout.write("Arc — slash-command interface\n\n")
    for category, cmds in by_cat.items():
        sys.stdout.write(f"{category}:\n")
        for cmd in cmds:
            alias_str = f"  (aliases: {', '.join(cmd.aliases)})" if cmd.aliases else ""
            hint = f" {cmd.args_hint}" if cmd.args_hint else ""
            sys.stdout.write(f"  /{cmd.name}{hint:<20}  {cmd.description}{alias_str}\n")
        sys.stdout.write("\n")


def _version_handler(args: list[str]) -> None:
    """Print arccli version."""
    import arccli

    sys.stdout.write(f"arccli {arccli.__version__}\n")


def _quit_handler(args: list[str]) -> None:
    """Exit the REPL or process."""
    raise SystemExit(0)


# ---------------------------------------------------------------------------
# Gateway pair command handlers (T1.8.2 — arc gateway pair approve/list/revoke)
# ---------------------------------------------------------------------------


def _load_pairing_store() -> object:
    """Build a PairingStore from the live gateway's own GatewayConfig.

    Using GatewayConfig.load() (not PairingStore()'s own hardcoded default)
    is what makes these CLI commands operate on the SAME SQLite db_path the
    running gateway daemon uses — the whole point of "approve must take
    effect on the live gateway". GatewayConfig.load() itself resolves
    ${ARC_CONFIG_DIR:-~/.arc}/gateway.toml, so an isolated ARC_CONFIG_DIR
    deployment stays consistent between the daemon and this CLI too.
    """
    from arcgateway.config import GatewayConfig
    from arcgateway.pairing import PairingStore

    config = GatewayConfig.load()
    return PairingStore(db_path=config.pairing.db_path, tier=config.gateway.tier)


def _gateway_pair_approve_handler(args: list[str]) -> None:
    """Approve a DM pairing code.

    Usage: arc gateway pair approve <code>

    Signs the pairing challenge with the operator's own standalone signing
    authority (``arc identity init``) and consumes the code. PairingStore
    requires a valid Ed25519 signature at EVERY tier — including personal,
    where the operator's self-signed key IS the trust anchor
    (arcgateway.pairing) — so this command cannot succeed without
    an identity that has been registered as a trusted operator, which
    ``arc identity init`` now does automatically.

    The handler delegates to arcgateway.pairing for the actual store lookup
    and approval so this module has no hard dependency on arcgateway at import.

    Args:
        args: Remaining CLI args after the command name. args[0] should be
              the 8-char pairing code.
    """
    if not args:
        sys.stderr.write(
            "Usage: gateway pair approve <code>\n"
            "  <code>  8-char pairing code sent to the user via DM\n"
        )
        sys.exit(1)

    code = args[0].strip().upper()
    if len(code) != 8:
        sys.stderr.write(f"Error: pairing codes are exactly 8 characters (got {len(code)})\n")
        sys.exit(1)

    from arccli.commands.identity import load_signing_authority

    identity = load_signing_authority()
    if identity is None:
        sys.stderr.write(
            "Error: no signing authority found. An operator must sign every pairing "
            "approval (all tiers — see arcgateway.pairing).\n"
            "Run: arc identity init\n"
        )
        sys.exit(1)

    import asyncio

    from arcgateway.pairing import PairingSignatureInvalid, build_pairing_challenge

    async def _approve() -> None:
        store = _load_pairing_store()
        pending = await store.list_pending()  # type: ignore[attr-defined]
        matched = next((pc for pc in pending if pc.code == code), None)
        if matched is None:
            sys.stderr.write(f"Error: code {code!r} is invalid, expired, or already consumed.\n")
            sys.exit(1)

        challenge = build_pairing_challenge(matched.code, matched.minted_at)
        signature = identity.sign(challenge)

        try:
            result = await store.verify_and_consume(  # type: ignore[attr-defined]
                code,
                approver_did=identity.did,
                signature=signature,
            )
        except PairingSignatureInvalid as exc:
            sys.stderr.write(f"Error: signature rejected — {exc}\n")
            sys.exit(1)

        if result is None:
            sys.stderr.write(f"Error: code {code!r} is invalid, expired, or already consumed.\n")
            sys.exit(1)
        sys.stdout.write(
            f"Approved: platform={result.platform!r} user_hash={result.platform_user_id_hash!r}\n"
        )

    asyncio.run(_approve())


def _gateway_pair_list_handler(args: list[str]) -> None:
    """List pending (unexpired, unconsumed) DM pairing codes.

    Usage: arc gateway pair list

    Displays all pending pairing codes across all platforms. Codes are
    identified by their code_id (sha256 first 16 chars) in audit logs —
    the raw code is shown here for operator action only.

    Args:
        args: Unused; no subcommand arguments accepted.
    """
    import asyncio

    async def _list() -> None:
        store = _load_pairing_store()
        pending = await store.list_pending()  # type: ignore[attr-defined]
        if not pending:
            sys.stdout.write("No pending pairing codes.\n")
            return
        sys.stdout.write(f"Pending pairing codes ({len(pending)}):\n")
        for pc in pending:
            import time

            remaining = max(0, pc.expires_at - time.time())
            sys.stdout.write(
                f"  {pc.code}  platform={pc.platform!r}  expires_in={int(remaining // 60)}m\n"
            )

    asyncio.run(_list())


def _gateway_pair_revoke_handler(args: list[str]) -> None:
    """Revoke a pending DM pairing code.

    Usage: arc gateway pair revoke <code>

    Invalidates the given pairing code so it can no longer be approved.
    Use this to cancel a code that was accidentally shared or has been
    compromised.

    Args:
        args: Remaining CLI args. args[0] should be the 8-char code to revoke.
    """
    if not args:
        sys.stderr.write(
            "Usage: gateway pair revoke <code>\n  <code>  8-char pairing code to invalidate\n"
        )
        sys.exit(1)

    code = args[0].strip().upper()

    import asyncio

    async def _revoke() -> None:
        store = _load_pairing_store()
        revoked = await store.revoke(code)  # type: ignore[attr-defined]
        if revoked:
            sys.stdout.write(f"Revoked: {code!r}\n")
        else:
            sys.stderr.write(f"Warning: code {code!r} was not found or already consumed.\n")

    asyncio.run(_revoke())


# ---------------------------------------------------------------------------
# Gateway adapter command handlers — install platform extension packages
# (arc gateway adapter list / install <name>)
# ---------------------------------------------------------------------------


def _gateway_adapter_list_handler(args: list[str]) -> None:
    """List official gateway adapter packages and their install status.

    Usage: arc gateway adapter list
    """
    from arcgateway.adapters.install import available_adapters, installed_adapters

    avail = available_adapters()
    installed = installed_adapters()
    sys.stdout.write("Official gateway adapters:\n")
    for name in sorted(avail):
        mark = "installed" if name in installed else "not installed"
        sys.stdout.write(f"  {name:<11} {avail[name]:<24} [{mark}]\n")
    sys.stdout.write("\nInstall one with: gateway adapter install <name>\n")


def _gateway_adapter_install_handler(args: list[str]) -> None:
    """Install an official gateway adapter extension package.

    Usage: arc gateway adapter install <name> [--upgrade]

    Pip/uv-installs ``arcgateway-<name>`` (the gateway discovers it via its
    entry point on next start). Only official adapter names are accepted.
    """
    from arcgateway.adapters.install import available_adapters, install_adapter

    avail = available_adapters()
    if not args:
        sys.stderr.write(
            "Usage: gateway adapter install <name> [--upgrade]\n"
            f"  <name>  one of: {', '.join(sorted(avail))}\n"
        )
        sys.exit(1)

    name = args[0].strip().lower()
    if name not in avail:
        sys.stderr.write(
            f"Error: unknown adapter {name!r}. Available: {', '.join(sorted(avail))}\n"
        )
        sys.exit(1)

    upgrade = "--upgrade" in args[1:] or "-U" in args[1:]
    dist = avail[name]
    sys.stdout.write(f"Installing {dist} ...\n")
    code = install_adapter(name, upgrade=upgrade)
    if code == 0:
        sys.stdout.write(
            f"Installed {dist}. Enable [platforms.{name}] in gateway.toml, "
            "set its token env var, and restart the gateway.\n"
        )
    else:
        sys.stderr.write(f"Error: installing {dist} failed (exit {code}).\n")
        sys.exit(code)


def _gateway_connect_telegram_handler(args: list[str]) -> None:
    """Guided Telegram connect — delegates to the gateway_connect module."""
    from arccli.commands.gateway_connect import gateway_connect_telegram_handler

    gateway_connect_telegram_handler(args)


def _gateway_connect_voice_handler(args: list[str]) -> None:
    """Wire the desk voice channel to an agent — delegates to gateway_connect."""
    from arccli.commands.gateway_connect import gateway_connect_voice_handler

    gateway_connect_voice_handler(args)


def _gateway_voice_engines_handler(args: list[str]) -> None:
    """List registered voice engines — delegates to gateway_connect."""
    from arccli.commands.gateway_connect import gateway_voice_engines_handler

    gateway_voice_engines_handler(args)


COMMAND_REGISTRY: list[CommandDef] = [
    # --- Info ---
    CommandDef(
        name="help",
        description="Show available commands and usage",
        category="Info",
        aliases=("?",),
        cli_only=True,
        handler=_help_handler,
    ),
    CommandDef(
        name="version",
        description="Show arccli version",
        category="Info",
        aliases=("ver",),
        handler=_version_handler,
    ),
    # --- Session ---
    CommandDef(
        name="agent",
        description="Agent management — create, build, chat, list",
        category="Session",
        args_hint="<subcommand>",
        handler=_agent_handler,
    ),
    CommandDef(
        name="run",
        description="Run prompts directly with arcrun (no agent directory)",
        category="Session",
        args_hint="<subcommand>",
        handler=_run_handler,
    ),
    # --- Configuration ---
    CommandDef(
        name="identity",
        description="Manage the signing authority for direct arcrun/arcllm runs",
        category="Configuration",
        args_hint="<init|show>",
        cli_only=True,
        handler=_identity_handler,
    ),
    CommandDef(
        name="init",
        description="Interactive setup wizard — tier-based configuration",
        category="Configuration",
        cli_only=True,
        handler=_init_handler,
    ),
    CommandDef(
        name="llm",
        description="ArcLLM commands — config, providers, models, calls",
        category="Configuration",
        args_hint="<subcommand>",
        handler=_llm_handler,
    ),
    # --- Tools & Skills ---
    CommandDef(
        name="skill",
        description="Skill management — list, create, validate, search",
        category="Tools & Skills",
        args_hint="<subcommand>",
        handler=_skill_handler,
    ),
    CommandDef(
        name="ext",
        description="Extension management — list, create, install, validate, inspect, verify",
        category="Tools & Skills",
        args_hint="<subcommand>",
        handler=_ext_handler,
    ),
    CommandDef(
        name="connector",
        description="Connect an agent to an external system — add, auth, list, probe, remove",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_connector_handler,
    ),
    CommandDef(
        name="keys",
        description="Provider API keys — list, set (hidden prompt), remove",
        category="Configuration",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_keys_handler,
    ),
    CommandDef(
        name="blueprint",
        description="Preset-config bootstrap — list, show, apply, verify, sign",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_blueprint_handler,
    ),
    CommandDef(
        name="user",
        description="Accounts that can sign in — add, list, passwd, role, telegram",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_user_handler,
    ),
    CommandDef(
        name="team",
        description="Team messaging — Slack for agents",
        category="Tools & Skills",
        args_hint="<subcommand>",
        handler=_team_handler,
    ),
    CommandDef(
        name="ui",
        description="ArcUI dashboard server",
        category="Tools & Skills",
        args_hint="<subcommand>",
        handler=_ui_handler,
    ),
    CommandDef(
        name="store",
        description="Operational store lifecycle — init, status, verify, backfill",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_store_handler,
    ),
    CommandDef(
        name="task",
        description="Mission-control tasks — create, list, edit, assign, complete, talk",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_task_handler,
    ),
    CommandDef(
        name="memory",
        description="Agent memory maintenance — dedup duplicates, status, backend health",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_memory_handler,
    ),
    CommandDef(
        name="module",
        description="Signed module bundles — list, bundle, install, remove",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_module_handler,
    ),
    CommandDef(
        name="install",
        description="Install every module each agent's config enables, then verify it",
        category="Session",
        args_hint="[--team-root <dir>]",
        cli_only=True,
        handler=_install_handler,
    ),
    CommandDef(
        name="runtime",
        description="Installed framework versions — list, and flip 'current' (update or rollback)",
        category="Configuration",
        args_hint="<list | activate <version>>",
        cli_only=True,
        handler=_runtime_handler,
    ),
    CommandDef(
        name="up",
        description="Bring up the whole stack — preflight, modules, verify, start",
        category="Session",
        args_hint="[--check] [--team-root <dir>]",
        cli_only=True,
        handler=_up_handler,
    ),
    CommandDef(
        name="restart",
        description="Restart the whole stack — arc + NATS + fleet + companions (--with-db bounces Postgres)",
        category="Session",
        aliases=("reboot",),
        args_hint="[--with-db] [--no-wait]",
        cli_only=True,
        handler=_restart_handler,
    ),
    CommandDef(
        name="prompt",
        description="View + edit/overwrite editable system prompts (list/show/diff/edit/reset)",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_prompt_handler,
    ),
    CommandDef(
        name="approve",
        description="Mechanical operator approval for blocked agent actions — list, <id>, --deny",
        category="Tools & Skills",
        args_hint="[list | <id> [--deny]]",
        cli_only=True,
        handler=_approve_handler,
    ),
    CommandDef(
        name="stop",
        description="Operator kill switch — stop a running agent run by run id or session",
        category="Tools & Skills",
        args_hint="[list | <run_id> [--reason ...] | --session <key>]",
        cli_only=True,
        handler=_stop_handler,
    ),
    CommandDef(
        name="trust",
        description="Operator approval for gated capabilities — list, approve, disapprove",
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_trust_handler,
    ),
    CommandDef(
        name="capability-import",
        description=(
            "Review and operator-promote staged capability ZIPs — "
            "import, list, show, edit, promote, revoke"
        ),
        category="Tools & Skills",
        args_hint="<subcommand>",
        aliases=("capability",),
        cli_only=True,
        handler=_capability_import_handler,
    ),
    CommandDef(
        name="workflow",
        description=(
            "ArcFlow — list, show, create, edit, archive, unarchive, purge, run, "
            "cancel, sign, verify"
        ),
        category="Tools & Skills",
        args_hint="<subcommand>",
        cli_only=True,
        handler=_workflow_handler,
    ),
    # --- Gateway pair commands (T1.8.2) ---
    # gateway_only=True: these commands only make sense on a running gateway.
    # cli_only=True: operator commands; must not appear in Telegram/Slack menus.
    # gateway_config_gate: rendered in gateway help only when pairing is enabled.
    CommandDef(
        name="gateway pair approve",
        description="Approve a DM pairing code sent to a user",
        category="Configuration",
        args_hint="<code>",
        cli_only=True,
        gateway_only=True,
        gateway_config_gate="security.require_pairing",
        handler=_gateway_pair_approve_handler,
    ),
    CommandDef(
        name="gateway pair list",
        description="List all pending DM pairing codes",
        category="Configuration",
        cli_only=True,
        gateway_only=True,
        gateway_config_gate="security.require_pairing",
        handler=_gateway_pair_list_handler,
    ),
    CommandDef(
        name="gateway pair revoke",
        description="Revoke a pending DM pairing code",
        category="Configuration",
        args_hint="<code>",
        cli_only=True,
        gateway_only=True,
        gateway_config_gate="security.require_pairing",
        handler=_gateway_pair_revoke_handler,
    ),
    CommandDef(
        name="gateway adapter list",
        description="List official gateway adapter packages and install status",
        category="Configuration",
        cli_only=True,
        gateway_only=True,
        handler=_gateway_adapter_list_handler,
    ),
    CommandDef(
        name="gateway adapter install",
        description="Install a gateway adapter package (telegram, slack, mattermost)",
        category="Configuration",
        args_hint="<name> [--upgrade]",
        cli_only=True,
        gateway_only=True,
        handler=_gateway_adapter_install_handler,
    ),
    CommandDef(
        name="gateway connect-telegram",
        description="Connect an agent to a Telegram bot (guided: paste token + your user ID)",
        category="Configuration",
        args_hint="--agent <dir> [--user-id <id>]",
        cli_only=True,
        handler=_gateway_connect_telegram_handler,
    ),
    CommandDef(
        name="gateway connect-voice",
        description="Connect an agent to the desk voice channel (hey Olivia, Kokoro voice)",
        category="Configuration",
        args_hint="--agent <dir> [--blend a:0.6,b:0.4] [--speed 1.12]",
        cli_only=True,
        handler=_gateway_connect_voice_handler,
    ),
    CommandDef(
        name="gateway voice engines",
        description="List registered voice engines (config-selectable by name)",
        category="Configuration",
        args_hint="",
        gateway_only=True,
        handler=_gateway_voice_engines_handler,
    ),
    # --- Exit ---
    CommandDef(
        name="quit",
        description="Exit the Arc REPL",
        category="Exit",
        aliases=("exit", "q", "bye"),
        cli_only=True,
        handler=_quit_handler,
    ),
]


# ---------------------------------------------------------------------------
# resolve_command — alias-aware lookup
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    """Strip leading slash and surrounding whitespace, lowercase."""
    return name.strip().lstrip("/").lower()


def resolve_command(name: str) -> CommandDef | None:
    """Return the CommandDef for *name* (canonical or alias), or None.

    Rules applied in order:
    1. Strip surrounding whitespace.
    2. Strip a single leading ``/``.
    3. Lowercase for comparison.
    4. Match canonical name first, then aliases.

    Parameters
    ----------
    name:
        Raw command token as typed by the user (e.g. ``"/help"``, ``"help"``,
        ``"?"``).

    Returns
    -------
    CommandDef | None
        The matching command descriptor, or ``None`` if unknown.
    """
    if not name or not name.strip():
        return None

    normalised = _normalise(name)
    if not normalised:
        return None

    # Two-pass: canonical names first (faster, more predictable)
    for cmd in COMMAND_REGISTRY:
        if cmd.name.lower() == normalised:
            return cmd

    # Second pass: aliases
    for cmd in COMMAND_REGISTRY:
        for alias in cmd.aliases:
            if alias.lower() == normalised:
                return cmd

    return None


def resolve_command_and_args(argv: list[str]) -> tuple[CommandDef | None, list[str]]:
    """Resolve a command from ``argv`` via longest-prefix matching.

    Registered command names may be multi-word (e.g. ``"gateway pair
    approve"``). A naive ``argv[0]``-only lookup can never match these
    unless the caller quotes the whole phrase as one shell token — which no
    real invocation, and no example in docs/cli.md, actually does (task
    #35: ``arc gateway pair approve CODE`` errored "unknown command
    'gateway'").

    Tries the longest possible word-count prefix of ``argv`` first, walking
    down to a single word, so ``arc gateway pair approve CODE`` resolves
    the same way the previously-required ``arc "gateway pair approve"
    CODE`` always did. Single-word commands are unaffected — the first
    (and only) prefix tried for a 1-word registered name is 1 word.

    Ambiguity is impossible by construction: ``COMMAND_REGISTRY`` is a
    static, known list, so for any given ``argv`` there is at most one
    exact-match name/alias at each prefix length, and the longest length is
    tried first — a shorter registered name can never shadow a longer,
    more specific match that also fits.

    Returns:
        ``(CommandDef, remaining_args)`` on match, or ``(None, [])`` when no
        prefix of ``argv`` matches any registered name or alias.
    """
    if not argv:
        return None, []
    max_words = max((len(cmd.name.split()) for cmd in COMMAND_REGISTRY), default=1)
    for word_count in range(min(max_words, len(argv)), 0, -1):
        candidate = " ".join(argv[:word_count])
        cmd = resolve_command(candidate)
        if cmd is not None:
            return cmd, argv[word_count:]
    return None, []


# --- Optional UI subcommands ------------------------------------------------
# arctui (the terminal UI) contributes the `tui` command by appending its
# CommandDef to COMMAND_REGISTRY when `arctui.entry` is imported. arccli cannot
# hard-depend on arctui (arctui depends on arccli), so we trigger that import
# here — after COMMAND_REGISTRY is defined, so the append lands cleanly — guarded
# so arccli works with or without arctui installed.
try:
    import arctui.entry  # noqa: F401  # registers the `tui` command on import
except ImportError:
    pass
