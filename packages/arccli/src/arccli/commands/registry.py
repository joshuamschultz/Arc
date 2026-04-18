"""Centralized slash command registry — arccli.commands.registry.

SDD §3.11: Single source of truth for arccli, arcgateway, and platform adapters.

Every slash command is described by a CommandDef. Handlers are attached lazily
at dispatch time to avoid circular imports and keep this module fast to import.
"""

from __future__ import annotations

import subprocess
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
#
# During the T1.1.5 migration phase, handlers delegate to main_legacy.py
# via subprocess so that legacy Click-based code is not imported into the
# new slash-command module namespace.
# ---------------------------------------------------------------------------


def _legacy_dispatch(group: str, args: list[str]) -> None:
    """Delegate a command to the legacy Click-based arc-legacy entry point.

    Uses subprocess to avoid importing Click-decorated modules into the
    slash-command namespace. Truthy exit codes are propagated.

    Security note: `sys.executable` is the current interpreter (trusted);
    `group` is a string literal from this module (not user input); `args`
    are forwarded as-is to the subprocess which applies its own validation.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "arccli.main_legacy", group, *args],
        check=False,
    )
    if result.returncode != 0:
        sys.exit(result.returncode)


def _agent_handler(args: list[str]) -> None:
    """Dispatch to arc agent subcommands.

    TODO(T1.1.5): Fully migrate agent.py to plain CommandDef handler.
    """
    _legacy_dispatch("agent", args)


def _llm_handler(args: list[str]) -> None:
    """Dispatch to arc llm subcommands.

    TODO(T1.1.5): Fully migrate llm.py to plain CommandDef handler.
    """
    _legacy_dispatch("llm", args)


def _run_handler(args: list[str]) -> None:
    """Dispatch to arc run subcommands.

    TODO(T1.1.5): Fully migrate run.py to plain CommandDef handler.
    """
    _legacy_dispatch("run", args)


def _skill_handler(args: list[str]) -> None:
    """Dispatch to arc skill subcommands.

    TODO(T1.1.5): Fully migrate skill.py to plain CommandDef handler.
    """
    _legacy_dispatch("skill", args)


def _team_handler(args: list[str]) -> None:
    """Dispatch to arc team subcommands.

    TODO(T1.1.5): Fully migrate team.py to plain CommandDef handler.
    """
    _legacy_dispatch("team", args)


def _ui_handler(args: list[str]) -> None:
    """Dispatch to arc ui subcommands.

    TODO(T1.1.5): Fully migrate ui.py to plain CommandDef handler.
    """
    _legacy_dispatch("ui", args)


def _ext_handler(args: list[str]) -> None:
    """Dispatch to arc ext subcommands.

    TODO(T1.1.5): Fully migrate ext.py to plain CommandDef handler.
    """
    _legacy_dispatch("ext", args)


def _init_handler(args: list[str]) -> None:
    """Dispatch to arc init wizard.

    TODO(T1.1.5): Fully migrate init_wizard.py to plain CommandDef handler.
    """
    _legacy_dispatch("init", args)


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


def _gateway_pair_approve_handler(args: list[str]) -> None:
    """Approve a DM pairing code.

    Usage: arc gateway pair approve <code>

    Marks the given 8-char pairing code as approved, which the gateway then
    uses to add the user to the session allowlist. The code is consumed on
    approval and cannot be reused.

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

    import asyncio

    from arcgateway.pairing import PairingStore

    async def _approve() -> None:
        store = PairingStore()
        result = await store.verify_and_consume(code)
        if result is None:
            sys.stderr.write(
                f"Error: code {code!r} is invalid, expired, or already consumed.\n"
            )
            sys.exit(1)
        sys.stdout.write(
            f"Approved: platform={result.platform!r} "
            f"user_hash={result.platform_user_id_hash!r}\n"
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

    from arcgateway.pairing import PairingStore

    async def _list() -> None:
        store = PairingStore()
        pending = await store.list_pending()
        if not pending:
            sys.stdout.write("No pending pairing codes.\n")
            return
        sys.stdout.write(f"Pending pairing codes ({len(pending)}):\n")
        for pc in pending:
            import time

            remaining = max(0, pc.expires_at - time.time())
            sys.stdout.write(
                f"  {pc.code}  platform={pc.platform!r}  "
                f"expires_in={int(remaining // 60)}m\n"
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
            "Usage: gateway pair revoke <code>\n"
            "  <code>  8-char pairing code to invalidate\n"
        )
        sys.exit(1)

    code = args[0].strip().upper()

    import asyncio

    from arcgateway.pairing import PairingStore

    async def _revoke() -> None:
        store = PairingStore()
        revoked = await store.revoke(code)
        if revoked:
            sys.stdout.write(f"Revoked: {code!r}\n")
        else:
            sys.stderr.write(
                f"Warning: code {code!r} was not found or already consumed.\n"
            )

    asyncio.run(_revoke())


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
        description="Run tasks directly with arcrun (no agent directory)",
        category="Session",
        args_hint="<subcommand>",
        handler=_run_handler,
    ),
    # --- Configuration ---
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
        description="Extension management — list, create, install, validate",
        category="Tools & Skills",
        args_hint="<subcommand>",
        handler=_ext_handler,
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
        gateway_config_gate="gateway.pairing.enabled",
        handler=_gateway_pair_approve_handler,
    ),
    CommandDef(
        name="gateway pair list",
        description="List all pending DM pairing codes",
        category="Configuration",
        cli_only=True,
        gateway_only=True,
        gateway_config_gate="gateway.pairing.enabled",
        handler=_gateway_pair_list_handler,
    ),
    CommandDef(
        name="gateway pair revoke",
        description="Revoke a pending DM pairing code",
        category="Configuration",
        args_hint="<code>",
        cli_only=True,
        gateway_only=True,
        gateway_config_gate="gateway.pairing.enabled",
        handler=_gateway_pair_revoke_handler,
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
