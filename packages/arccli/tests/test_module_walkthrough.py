"""Tests for the interactive module walkthrough."""

from __future__ import annotations

import tomllib
from unittest.mock import patch

from arccli.module_walkthrough import (
    _MODULE_REGISTRY,
    _prompt_module,
    _toml_val,
    format_modules_toml,
    walk_modules,
)


class TestFormatModulesToml:
    """Tests for TOML generation."""

    def test_single_module_with_config(self):
        modules = {
            "memory": {
                "enabled": True,
                "config": {
                    "context_budget_tokens": 2000,
                    "entity_extraction_enabled": True,
                },
            },
        }
        result = format_modules_toml(modules)
        assert "[modules.memory]" in result
        assert "enabled = true" in result
        assert "[modules.memory.config]" in result
        assert "context_budget_tokens = 2000" in result
        assert "entity_extraction_enabled = true" in result

    def test_module_without_config(self):
        modules = {
            "telegram": {"enabled": True, "config": {}},
        }
        result = format_modules_toml(modules)
        assert "[modules.telegram]" in result
        assert "enabled = true" in result
        assert "[modules.telegram.config]" not in result

    def test_multiple_modules(self):
        modules = {
            "memory": {"enabled": True, "config": {"context_budget_tokens": 4000}},
            "policy": {"enabled": True, "config": {"eval_interval_turns": 10}},
        }
        result = format_modules_toml(modules)
        assert "[modules.memory]" in result
        assert "[modules.policy]" in result
        assert "context_budget_tokens = 4000" in result
        assert "eval_interval_turns = 10" in result

    def test_empty_modules(self):
        result = format_modules_toml({})
        assert result == ""

    def test_output_is_valid_toml(self):
        modules = {
            "memory": {
                "enabled": True,
                "config": {
                    "context_budget_tokens": 2000,
                    "entity_extraction_enabled": True,
                },
            },
            "policy": {
                "enabled": True,
                "config": {"eval_interval_turns": 5},
            },
            "scheduler": {
                "enabled": True,
                "config": {"check_interval_seconds": 30},
            },
        }
        toml_text = format_modules_toml(modules)
        parsed = tomllib.loads(toml_text)
        assert parsed["modules"]["memory"]["enabled"] is True
        assert parsed["modules"]["memory"]["config"]["context_budget_tokens"] == 2000
        assert parsed["modules"]["policy"]["config"]["eval_interval_turns"] == 5


class TestTomlVal:
    """Tests for TOML value formatting."""

    def test_bool_true(self):
        assert _toml_val(True) == "true"

    def test_bool_false(self):
        assert _toml_val(False) == "false"

    def test_int(self):
        assert _toml_val(42) == "42"

    def test_float(self):
        assert _toml_val(3.14) == "3.14"

    def test_string(self):
        assert _toml_val("hello") == '"hello"'


class TestPromptModule:
    """Tests for individual module prompting."""

    def test_disabled_returns_none(self):
        info = _MODULE_REGISTRY["memory"]
        with patch("click.confirm", return_value=False):
            result = _prompt_module("memory", info, {})
        assert result is None

    def test_enabled_with_defaults(self):
        info = _MODULE_REGISTRY["memory"]
        with (
            patch("click.confirm", return_value=True),
            patch("click.prompt", side_effect=[2000]),
        ):
            result = _prompt_module("memory", info, {})
        assert result is not None
        assert result["enabled"] is True
        assert "config" in result

    def test_bool_prompt(self):
        info = _MODULE_REGISTRY["browser"]
        # First confirm = enable, second confirm = headless
        confirms = iter([True, True])
        with patch("click.confirm", side_effect=confirms):
            result = _prompt_module("browser", info, {})
        assert result is not None
        assert result["config"]["headless"] is True

    def test_int_prompt(self):
        info = _MODULE_REGISTRY["scheduler"]
        with (
            patch("click.confirm", return_value=True),
            patch("click.prompt", return_value=60),
        ):
            result = _prompt_module("scheduler", info, {})
        assert result is not None
        assert result["config"]["check_interval_seconds"] == 60

    def test_existing_config_used_as_default(self):
        info = _MODULE_REGISTRY["policy"]
        current = {"enabled": True, "config": {"eval_interval_turns": 10}}
        with (
            patch("click.confirm", return_value=True),
            patch("click.prompt", return_value=10) as mock_prompt,
        ):
            _prompt_module("policy", info, current)
        # The default passed to click.prompt should be the existing value
        mock_prompt.assert_called_once()
        call_kwargs = mock_prompt.call_args
        assert call_kwargs[1]["default"] == 10

    def test_telegram_shows_hint(self):
        info = _MODULE_REGISTRY["telegram"]
        with patch("click.confirm", return_value=True):
            with patch("arccli.module_walkthrough.click_echo") as mock_echo:
                _prompt_module("telegram", info, {})
        # Should have printed the setup hint
        hint_calls = [c for c in mock_echo.call_args_list if "setup-telegram" in str(c)]
        assert len(hint_calls) > 0


class TestWalkModules:
    """Tests for the full walkthrough."""

    def test_all_defaults_accepted(self):
        # Accept defaults for all modules (y for enabled-by-default, n for disabled)
        confirms = iter(
            [
                True,  # memory enable
                True,  # memory entity_extraction
                True,  # policy enable
                True,  # scheduler enable
                False,  # browser disable
                False,  # telegram disable
            ]
        )
        prompts = iter(
            [
                2000,  # memory context_budget_tokens
                5,  # policy eval_interval_turns
                30,  # scheduler check_interval_seconds
            ]
        )
        with (
            patch("click.confirm", side_effect=confirms),
            patch("click.prompt", side_effect=prompts),
        ):
            result = walk_modules({})

        assert "memory" in result
        assert "policy" in result
        assert "scheduler" in result
        assert "browser" not in result
        assert "telegram" not in result

    def test_enable_disabled_module(self):
        # Enable browser which is disabled by default
        confirms = iter(
            [
                False,  # memory disable
                False,  # policy disable
                False,  # scheduler disable
                True,  # browser enable
                True,  # browser headless
                False,  # telegram disable
            ]
        )
        with (
            patch("click.confirm", side_effect=confirms),
            patch("click.prompt", side_effect=[]),
        ):
            result = walk_modules({})

        assert "browser" in result
        assert result["browser"]["config"]["headless"] is True
        assert "memory" not in result

    def test_registry_has_all_expected_modules(self):
        expected = {"memory", "policy", "scheduler", "browser", "telegram"}
        assert set(_MODULE_REGISTRY.keys()) == expected
