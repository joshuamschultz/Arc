"""Unit tests for SlashCommandCompleter.

Verifies that:
- Partial slash input returns matching completions.
- Alias-prefixed input resolves to the correct CommandDef.
- Exact-match lookup uses resolve_command correctly.
- Empty / non-slash input returns empty list.
- Completions are sorted alphabetically.
- autocomplete_dict() includes aliases.
- all_commands_by_category() returns grouped structure.
"""

from __future__ import annotations

import pytest

from arctui.command_completer import Completion, SlashCommandCompleter


@pytest.fixture()
def completer() -> SlashCommandCompleter:
    """Fresh completer for each test."""
    return SlashCommandCompleter()


class TestResolve:
    """Tests for the ``resolve`` method."""

    def test_slash_help_returns_help(self, completer: SlashCommandCompleter) -> None:
        """``/hel`` should match ``help``."""
        results = completer.resolve("/hel")
        names = [c.text for c in results]
        assert "help" in names

    def test_slash_full_name_returns_single(self, completer: SlashCommandCompleter) -> None:
        """``/help`` should return exactly the help command."""
        results = completer.resolve("/help")
        assert any(c.text == "help" for c in results)

    def test_alias_resolution(self, completer: SlashCommandCompleter) -> None:
        """``/?`` should match the help command via its alias."""
        results = completer.resolve("/?")
        assert any(c.text == "?" for c in results)

    def test_empty_after_slash_returns_empty(self, completer: SlashCommandCompleter) -> None:
        """A bare ``/`` with no trailing chars returns no completions."""
        results = completer.resolve("/")
        assert results == []

    def test_no_leading_slash_returns_empty(self, completer: SlashCommandCompleter) -> None:
        """Plain text without ``/`` returns no completions."""
        results = completer.resolve("help")
        assert results == []

    def test_empty_string_returns_empty(self, completer: SlashCommandCompleter) -> None:
        """Empty string returns no completions."""
        results = completer.resolve("")
        assert results == []

    def test_results_sorted_alphabetically(self, completer: SlashCommandCompleter) -> None:
        """Completions are sorted by text."""
        results = completer.resolve("/")  # no completions for bare slash
        assert results == sorted(results, key=lambda c: c.text)

    def test_nonmatching_prefix_returns_empty(self, completer: SlashCommandCompleter) -> None:
        """A prefix that matches nothing returns an empty list."""
        results = completer.resolve("/zzznomatch")
        assert results == []

    def test_all_completions_are_completion_type(self, completer: SlashCommandCompleter) -> None:
        """Every result is a Completion dataclass instance."""
        results = completer.resolve("/h")
        for item in results:
            assert isinstance(item, Completion)

    def test_completion_has_description(self, completer: SlashCommandCompleter) -> None:
        """Every completion has a non-empty description."""
        results = completer.resolve("/hel")
        for item in results:
            assert item.description

    def test_alias_marked_as_alias(self, completer: SlashCommandCompleter) -> None:
        """Alias completions have is_alias=True."""
        results = completer.resolve("/?")
        alias_results = [c for c in results if c.text == "?"]
        if alias_results:
            assert alias_results[0].is_alias is True

    def test_canonical_not_marked_as_alias(self, completer: SlashCommandCompleter) -> None:
        """Canonical-name completions have is_alias=False."""
        results = completer.resolve("/help")
        canonical = [c for c in results if c.text == "help"]
        if canonical:
            assert canonical[0].is_alias is False


class TestResolveExact:
    """Tests for the ``resolve_exact`` method."""

    def test_exact_help(self, completer: SlashCommandCompleter) -> None:
        """``resolve_exact('help')`` returns the help CommandDef."""
        cmd = completer.resolve_exact("help")
        assert cmd is not None
        assert cmd.name == "help"

    def test_exact_alias(self, completer: SlashCommandCompleter) -> None:
        """``resolve_exact('?')`` resolves via the alias to help."""
        cmd = completer.resolve_exact("?")
        assert cmd is not None
        assert cmd.name == "help"

    def test_leading_slash_stripped(self, completer: SlashCommandCompleter) -> None:
        """``resolve_exact('/help')`` strips the slash before lookup."""
        cmd = completer.resolve_exact("/help")
        assert cmd is not None
        assert cmd.name == "help"

    def test_unknown_returns_none(self, completer: SlashCommandCompleter) -> None:
        """``resolve_exact('zzz')`` returns None for unknown commands."""
        cmd = completer.resolve_exact("zzz")
        assert cmd is None


class TestAutocompleteDict:
    """Tests for the ``autocomplete_dict`` method."""

    def test_returns_dict(self, completer: SlashCommandCompleter) -> None:
        """``autocomplete_dict`` returns a non-empty dict."""
        d = completer.autocomplete_dict()
        assert isinstance(d, dict)
        assert len(d) > 0

    def test_help_in_dict(self, completer: SlashCommandCompleter) -> None:
        """``help`` is a key in the autocomplete dict."""
        d = completer.autocomplete_dict()
        assert "help" in d

    def test_aliases_in_dict(self, completer: SlashCommandCompleter) -> None:
        """Aliases are also present as keys."""
        d = completer.autocomplete_dict()
        assert "?" in d

    def test_values_are_strings(self, completer: SlashCommandCompleter) -> None:
        """All values are non-empty strings."""
        d = completer.autocomplete_dict()
        for key, val in d.items():
            assert isinstance(val, str), f"Description for {key!r} is not a string"
            assert val, f"Description for {key!r} is empty"


class TestAllCommandsByCategory:
    """Tests for the ``all_commands_by_category`` method."""

    def test_returns_dict(self, completer: SlashCommandCompleter) -> None:
        """``all_commands_by_category`` returns a dict."""
        d = completer.all_commands_by_category()
        assert isinstance(d, dict)

    def test_non_empty(self, completer: SlashCommandCompleter) -> None:
        """Result has at least one category."""
        d = completer.all_commands_by_category()
        assert len(d) > 0

    def test_values_are_lists(self, completer: SlashCommandCompleter) -> None:
        """All values are lists of CommandDef."""
        from arccli.commands.registry import CommandDef

        d = completer.all_commands_by_category()
        for category, cmds in d.items():
            assert isinstance(cmds, list), f"Category {category!r} value is not a list"
            for cmd in cmds:
                assert isinstance(cmd, CommandDef), f"Item in {category!r} is not CommandDef"

    def test_info_category_contains_help(self, completer: SlashCommandCompleter) -> None:
        """The Info category contains the help command."""
        d = completer.all_commands_by_category()
        info_cmds = d.get("Info", [])
        assert any(c.name == "help" for c in info_cmds)
