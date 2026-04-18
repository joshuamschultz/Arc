"""Render helpers for arccli.commands — SDD §3.11, six consumers.

All helpers read from COMMAND_REGISTRY at call time so that any dynamic
additions before render are reflected without re-importing this module.
"""

from __future__ import annotations

from arccli.commands.registry import COMMAND_REGISTRY, CommandDef

# ---------------------------------------------------------------------------
# 1. CLI help — commands_by_category()
# ---------------------------------------------------------------------------


def commands_by_category() -> dict[str, list[CommandDef]]:
    """Return a dict mapping category name to list of CommandDef.

    The order of keys follows the canonical category order defined in SDD §3.11.
    All registry commands are included (including cli_only ones — callers filter
    as needed).

    Returns
    -------
    dict[str, list[CommandDef]]
        Keys are category literals; values preserve registry insertion order.
    """
    # Canonical category order from SDD §3.11
    ordered: dict[str, list[CommandDef]] = {
        "Session": [],
        "Configuration": [],
        "Tools & Skills": [],
        "Info": [],
        "Exit": [],
    }
    for cmd in COMMAND_REGISTRY:
        ordered[cmd.category].append(cmd)
    # Remove empty categories to keep output clean
    return {cat: cmds for cat, cmds in ordered.items() if cmds}


# ---------------------------------------------------------------------------
# 2. arcgateway help — gateway_help_lines()
# ---------------------------------------------------------------------------


def gateway_help_lines() -> list[str]:
    """Return help text lines suitable for gateway/bot ``/help`` responses.

    Excludes ``cli_only`` commands. Each line has the format::

        /name [args_hint]  — description

    Returns
    -------
    list[str]
        One line per non-cli_only command.
    """
    lines: list[str] = []
    for cmd in COMMAND_REGISTRY:
        if cmd.cli_only:
            continue
        hint = f" {cmd.args_hint}" if cmd.args_hint else ""
        lines.append(f"/{cmd.name}{hint}  — {cmd.description}")
    return lines


# ---------------------------------------------------------------------------
# 3. Telegram BotCommand menu — telegram_bot_commands()
# ---------------------------------------------------------------------------


def telegram_bot_commands() -> list[dict[str, str]]:
    """Return a list of Telegram ``BotCommand``-style dicts.

    Excludes ``cli_only`` commands. The ``command`` field has no leading slash
    (Telegram adds it in the UI). Description is truncated to 256 chars per
    Telegram's API limit.

    Returns
    -------
    list[dict[str, str]]
        Each dict has ``"command"`` and ``"description"`` keys.
    """
    result: list[dict[str, str]] = []
    for cmd in COMMAND_REGISTRY:
        if cmd.cli_only:
            continue
        result.append(
            {
                "command": cmd.name,  # no leading slash — Telegram adds it
                "description": cmd.description[:256],
            }
        )
    return result


# ---------------------------------------------------------------------------
# 4. Slack subcommand routing — slack_subcommand_map()
# ---------------------------------------------------------------------------


def slack_subcommand_map() -> dict[str, str]:
    """Return a mapping of subcommand name -> description for Slack routing.

    Excludes ``cli_only`` commands.

    Returns
    -------
    dict[str, str]
        ``{canonical_name: description}`` for all non-cli_only commands.
    """
    return {cmd.name: cmd.description for cmd in COMMAND_REGISTRY if not cmd.cli_only}


# ---------------------------------------------------------------------------
# 5. Autocomplete dict — autocomplete_dict()
# ---------------------------------------------------------------------------


def autocomplete_dict() -> dict[str, str]:
    """Return a flat dict mapping every name/alias to its description.

    Used by ``SlashCommandCompleter`` (arctui) and arccli's prompt_toolkit
    completer. Aliases map to the same description as the canonical command.

    Returns
    -------
    dict[str, str]
        ``{name_or_alias: description}`` for all commands and their aliases.
    """
    result: dict[str, str] = {}
    for cmd in COMMAND_REGISTRY:
        result[cmd.name] = cmd.description
        for alias in cmd.aliases:
            result[alias] = cmd.description
    return result
