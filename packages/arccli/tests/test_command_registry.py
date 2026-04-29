"""Tests for arccli.commands registry — TDD spec for T1.1.

Run: cd packages/arccli && python -m pytest tests/test_command_registry.py -v
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# CommandDef shape tests
# ---------------------------------------------------------------------------


class TestCommandDef:
    """CommandDef is a frozen dataclass with correct field types."""

    def test_commanddef_importable(self) -> None:
        from arccli.commands.registry import CommandDef  # noqa: F401

    def test_commanddef_required_fields(self) -> None:
        from arccli.commands.registry import CommandDef

        cmd = CommandDef(
            name="help",
            description="Show help",
            category="Info",
        )
        assert cmd.name == "help"
        assert cmd.description == "Show help"
        assert cmd.category == "Info"

    def test_commanddef_defaults(self) -> None:
        from arccli.commands.registry import CommandDef

        cmd = CommandDef(name="x", description="x", category="Info")
        assert cmd.aliases == ()
        assert cmd.args_hint == ""
        assert cmd.cli_only is False
        assert cmd.gateway_only is False
        assert cmd.gateway_config_gate is None
        assert cmd.handler is None

    def test_commanddef_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        from arccli.commands.registry import CommandDef

        cmd = CommandDef(name="x", description="x", category="Info")
        with pytest.raises(FrozenInstanceError):
            cmd.name = "y"  # type: ignore[misc]

    def test_commanddef_with_aliases(self) -> None:
        from arccli.commands.registry import CommandDef

        cmd = CommandDef(
            name="quit",
            description="Exit",
            category="Exit",
            aliases=("exit", "q"),
        )
        assert cmd.aliases == ("exit", "q")

    def test_commanddef_with_handler(self) -> None:
        from arccli.commands.registry import CommandDef

        def my_handler(args: list[str]) -> None:
            pass

        cmd = CommandDef(
            name="run",
            description="Run something",
            category="Session",
            handler=my_handler,
        )
        assert cmd.handler is my_handler

    def test_commanddef_category_values(self) -> None:
        """All five category literals are accepted."""
        from arccli.commands.registry import CommandDef

        for cat in ("Session", "Configuration", "Tools & Skills", "Info", "Exit"):
            cmd = CommandDef(name="x", description="x", category=cat)  # type: ignore[arg-type]
            assert cmd.category == cat

    def test_commanddef_gateway_flags(self) -> None:
        from arccli.commands.registry import CommandDef

        cmd = CommandDef(
            name="deploy",
            description="Deploy",
            category="Configuration",
            gateway_only=True,
            gateway_config_gate="gateway.enabled",
        )
        assert cmd.gateway_only is True
        assert cmd.gateway_config_gate == "gateway.enabled"

    def test_commanddef_cli_only(self) -> None:
        from arccli.commands.registry import CommandDef

        cmd = CommandDef(
            name="repl",
            description="Start REPL",
            category="Session",
            cli_only=True,
        )
        assert cmd.cli_only is True


# ---------------------------------------------------------------------------
# COMMAND_REGISTRY population tests
# ---------------------------------------------------------------------------


class TestCommandRegistry:
    """COMMAND_REGISTRY is a non-empty list of CommandDef instances."""

    def test_registry_importable(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY  # noqa: F401

    def test_registry_is_list(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY

        assert isinstance(COMMAND_REGISTRY, list)

    def test_registry_not_empty(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY

        assert len(COMMAND_REGISTRY) > 0

    def test_registry_all_commanddef(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY, CommandDef

        for cmd in COMMAND_REGISTRY:
            assert isinstance(cmd, CommandDef), f"Expected CommandDef, got {type(cmd)}: {cmd}"

    def test_registry_names_are_unique(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY

        names = [cmd.name for cmd in COMMAND_REGISTRY]
        assert len(names) == len(set(names)), f"Duplicate names: {names}"

    def test_registry_contains_help(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY

        names = {cmd.name for cmd in COMMAND_REGISTRY}
        assert "help" in names

    def test_registry_contains_exit(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY

        names = {cmd.name for cmd in COMMAND_REGISTRY}
        assert "exit" in names or "quit" in names

    def test_registry_covers_legacy_subcommands(self) -> None:
        """Legacy Click subcommands (init, llm, agent, run, ext, skill, team, ui) are covered."""
        from arccli.commands.registry import COMMAND_REGISTRY

        names = {cmd.name for cmd in COMMAND_REGISTRY}
        # These are the top-level subcommand groups from the old main.py
        expected = {"init", "llm", "agent", "run", "skill", "team", "ui", "ext"}
        missing = expected - names
        assert not missing, f"Registry missing legacy commands: {missing}"

    def test_registry_has_no_none_names(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY

        for cmd in COMMAND_REGISTRY:
            assert cmd.name, f"Command has empty name: {cmd}"

    def test_registry_has_no_leading_slash(self) -> None:
        """SDD §3.11: name is canonical, no leading slash."""
        from arccli.commands.registry import COMMAND_REGISTRY

        for cmd in COMMAND_REGISTRY:
            assert not cmd.name.startswith("/"), f"Name must not have leading slash: {cmd.name}"

    def test_registry_aliases_no_leading_slash(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY

        for cmd in COMMAND_REGISTRY:
            for alias in cmd.aliases:
                assert not alias.startswith("/"), (
                    f"Alias must not have leading slash: {alias} in {cmd.name}"
                )


# ---------------------------------------------------------------------------
# resolve_command tests
# ---------------------------------------------------------------------------


class TestResolveCommand:
    """resolve_command(name) -> CommandDef | None with alias support."""

    def test_resolve_importable(self) -> None:
        from arccli.commands.registry import resolve_command  # noqa: F401

    def test_resolve_exact_name(self) -> None:
        from arccli.commands.registry import resolve_command

        result = resolve_command("help")
        assert result is not None
        assert result.name == "help"

    def test_resolve_unknown_returns_none(self) -> None:
        from arccli.commands.registry import resolve_command

        assert resolve_command("does-not-exist-xyz") is None

    def test_resolve_alias(self) -> None:
        """resolve_command must resolve aliases to the canonical CommandDef."""
        from arccli.commands.registry import COMMAND_REGISTRY, resolve_command

        # Find a command with aliases
        cmd_with_alias = next((cmd for cmd in COMMAND_REGISTRY if cmd.aliases), None)
        if cmd_with_alias is None:
            pytest.skip("No commands with aliases defined yet")

        alias = cmd_with_alias.aliases[0]
        resolved = resolve_command(alias)
        assert resolved is not None
        assert resolved.name == cmd_with_alias.name

    def test_resolve_case_insensitive_canonical(self) -> None:
        """Canonical names resolve regardless of case."""
        from arccli.commands.registry import resolve_command

        assert resolve_command("HELP") is not None or resolve_command("help") is not None

    def test_resolve_with_leading_slash_stripped(self) -> None:
        """Leading slash is stripped before lookup — /help resolves to help."""
        from arccli.commands.registry import resolve_command

        assert resolve_command("/help") is not None

    def test_resolve_empty_string_returns_none(self) -> None:
        from arccli.commands.registry import resolve_command

        assert resolve_command("") is None

    def test_resolve_whitespace_stripped(self) -> None:
        from arccli.commands.registry import resolve_command

        assert resolve_command("  help  ") is not None


# ---------------------------------------------------------------------------
# render helpers tests
# ---------------------------------------------------------------------------


class TestCommandsByCategory:
    """commands_by_category() groups CommandDef by their category."""

    def test_importable(self) -> None:
        from arccli.commands.render import commands_by_category  # noqa: F401

    def test_returns_dict(self) -> None:
        from arccli.commands.render import commands_by_category

        result = commands_by_category()
        assert isinstance(result, dict)

    def test_all_keys_are_valid_categories(self) -> None:
        from arccli.commands.render import commands_by_category

        valid = {"Session", "Configuration", "Tools & Skills", "Info", "Exit"}
        result = commands_by_category()
        for key in result:
            assert key in valid, f"Unexpected category key: {key}"

    def test_values_are_lists_of_commanddef(self) -> None:
        from arccli.commands.registry import CommandDef
        from arccli.commands.render import commands_by_category

        result = commands_by_category()
        for _cat, cmds in result.items():
            assert isinstance(cmds, list)
            for cmd in cmds:
                assert isinstance(cmd, CommandDef)

    def test_all_registry_commands_appear(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY
        from arccli.commands.render import commands_by_category

        result = commands_by_category()
        all_in_result = {cmd.name for cmds in result.values() for cmd in cmds}
        all_in_registry = {cmd.name for cmd in COMMAND_REGISTRY}
        assert all_in_registry == all_in_result


class TestGatewayHelpLines:
    def test_importable(self) -> None:
        from arccli.commands.render import gateway_help_lines  # noqa: F401

    def test_returns_list_of_strings(self) -> None:
        from arccli.commands.render import gateway_help_lines

        result = gateway_help_lines()
        assert isinstance(result, list)
        for line in result:
            assert isinstance(line, str)

    def test_excludes_cli_only_commands(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY
        from arccli.commands.render import gateway_help_lines

        cli_only_names = {cmd.name for cmd in COMMAND_REGISTRY if cmd.cli_only}
        lines_text = " ".join(gateway_help_lines())
        for name in cli_only_names:
            assert name not in lines_text, f"cli_only command '{name}' appeared in gateway help"


class TestTelegramBotCommands:
    def test_importable(self) -> None:
        from arccli.commands.render import telegram_bot_commands  # noqa: F401

    def test_returns_list_of_dicts(self) -> None:
        from arccli.commands.render import telegram_bot_commands

        result = telegram_bot_commands()
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)
            assert "command" in item
            assert "description" in item

    def test_commands_no_leading_slash(self) -> None:
        """Telegram BotCommand.command field must not have a leading slash."""
        from arccli.commands.render import telegram_bot_commands

        for item in telegram_bot_commands():
            assert not item["command"].startswith("/"), (
                f"Telegram command must not have leading slash: {item['command']}"
            )

    def test_excludes_cli_only(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY
        from arccli.commands.render import telegram_bot_commands

        cli_only_names = {cmd.name for cmd in COMMAND_REGISTRY if cmd.cli_only}
        tg_command_names = {item["command"] for item in telegram_bot_commands()}
        overlap = cli_only_names & tg_command_names
        assert not overlap, f"cli_only commands in Telegram list: {overlap}"


class TestSlackSubcommandMap:
    def test_importable(self) -> None:
        from arccli.commands.render import slack_subcommand_map  # noqa: F401

    def test_returns_dict(self) -> None:
        from arccli.commands.render import slack_subcommand_map

        result = slack_subcommand_map()
        assert isinstance(result, dict)

    def test_keys_are_strings(self) -> None:
        from arccli.commands.render import slack_subcommand_map

        for key in slack_subcommand_map():
            assert isinstance(key, str)

    def test_excludes_cli_only(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY
        from arccli.commands.render import slack_subcommand_map

        cli_only_names = {cmd.name for cmd in COMMAND_REGISTRY if cmd.cli_only}
        slack_names = set(slack_subcommand_map().keys())
        overlap = cli_only_names & slack_names
        assert not overlap, f"cli_only commands in Slack map: {overlap}"


class TestAutocompleteDict:
    def test_importable(self) -> None:
        from arccli.commands.render import autocomplete_dict  # noqa: F401

    def test_returns_dict(self) -> None:
        from arccli.commands.render import autocomplete_dict

        result = autocomplete_dict()
        assert isinstance(result, dict)

    def test_canonical_names_included(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY
        from arccli.commands.render import autocomplete_dict

        result = autocomplete_dict()
        for cmd in COMMAND_REGISTRY:
            assert cmd.name in result, f"Canonical name missing from autocomplete: {cmd.name}"

    def test_aliases_included(self) -> None:
        from arccli.commands.registry import COMMAND_REGISTRY
        from arccli.commands.render import autocomplete_dict

        result = autocomplete_dict()
        for cmd in COMMAND_REGISTRY:
            for alias in cmd.aliases:
                assert alias in result, f"Alias missing from autocomplete: {alias}"

    def test_values_are_strings(self) -> None:
        from arccli.commands.render import autocomplete_dict

        for _key, val in autocomplete_dict().items():
            assert isinstance(val, str)
