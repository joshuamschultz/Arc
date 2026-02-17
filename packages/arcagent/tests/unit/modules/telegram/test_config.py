"""Unit tests for TelegramConfig — S005 Phase 1."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.modules.telegram.config import TelegramConfig


class TestTelegramConfigDefaults:
    def test_defaults(self) -> None:
        cfg = TelegramConfig()
        assert cfg.enabled is False
        assert cfg.allowed_chat_ids == []
        assert cfg.poll_interval == 1.0
        assert cfg.max_message_length == 4096
        assert cfg.bot_token_env_var == "ARCAGENT_TELEGRAM_BOT_TOKEN"

    def test_custom_values(self) -> None:
        cfg = TelegramConfig(
            enabled=True,
            allowed_chat_ids=[123, 456],
            poll_interval=2.5,
            max_message_length=2000,
            bot_token_env_var="CUSTOM_TOKEN",
        )
        assert cfg.enabled is True
        assert cfg.allowed_chat_ids == [123, 456]
        assert cfg.poll_interval == 2.5
        assert cfg.max_message_length == 2000
        assert cfg.bot_token_env_var == "CUSTOM_TOKEN"


class TestTelegramConfigValidation:
    def test_extra_fields_forbidden(self) -> None:
        """ModuleConfig uses extra='forbid' to catch typos."""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            TelegramConfig(typo_field="oops")

    def test_empty_allowed_chat_ids_is_valid(self) -> None:
        """Empty list = accept no messages (fail-closed)."""
        cfg = TelegramConfig(allowed_chat_ids=[])
        assert cfg.allowed_chat_ids == []

    def test_from_dict(self) -> None:
        """Config can be constructed from dict (module loader pattern)."""
        data = {"enabled": True, "allowed_chat_ids": [999]}
        cfg = TelegramConfig(**data)
        assert cfg.enabled is True
        assert cfg.allowed_chat_ids == [999]
