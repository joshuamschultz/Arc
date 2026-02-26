"""Unit tests for SlackConfig — SPEC-011 Phase 1."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.modules.slack.config import SlackConfig


class TestSlackConfigDefaults:
    def test_defaults(self) -> None:
        cfg = SlackConfig()
        assert cfg.enabled is False
        assert cfg.allowed_user_ids == []
        assert cfg.max_message_length == 4000
        assert cfg.bot_token_env_var == "ARCAGENT_SLACK_BOT_TOKEN"
        assert cfg.app_token_env_var == "ARCAGENT_SLACK_APP_TOKEN"

    def test_custom_values(self) -> None:
        cfg = SlackConfig(
            enabled=True,
            allowed_user_ids=["U12345678", "U87654321"],
            max_message_length=3000,
            bot_token_env_var="CUSTOM_BOT_TOKEN",
            app_token_env_var="CUSTOM_APP_TOKEN",
        )
        assert cfg.enabled is True
        assert cfg.allowed_user_ids == ["U12345678", "U87654321"]
        assert cfg.max_message_length == 3000
        assert cfg.bot_token_env_var == "CUSTOM_BOT_TOKEN"
        assert cfg.app_token_env_var == "CUSTOM_APP_TOKEN"


class TestSlackConfigValidation:
    def test_extra_fields_forbidden(self) -> None:
        """ModuleConfig uses extra='forbid' to catch typos."""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            SlackConfig(typo_field="oops")

    def test_empty_allowed_user_ids_is_valid(self) -> None:
        """Empty list = accept no messages (fail-closed)."""
        cfg = SlackConfig(allowed_user_ids=[])
        assert cfg.allowed_user_ids == []

    def test_from_dict(self) -> None:
        """Config can be constructed from dict (module loader pattern)."""
        data = {"enabled": True, "allowed_user_ids": ["U999"]}
        cfg = SlackConfig(**data)
        assert cfg.enabled is True
        assert cfg.allowed_user_ids == ["U999"]
