"""Unit tests for messaging module configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.modules.messaging.config import MessagingConfig


class TestMessagingConfig:
    def test_defaults(self) -> None:
        cfg = MessagingConfig()
        assert cfg.enabled is False
        assert cfg.entity_id == ""
        assert cfg.roles == []
        assert cfg.capabilities == []
        assert cfg.auto_register is True
        assert cfg.poll_interval_seconds == 5.0
        assert cfg.auto_ack is True
        assert cfg.max_messages_per_poll == 20

    def test_custom_values(self) -> None:
        cfg = MessagingConfig(
            enabled=True,
            entity_id="agent://brad",
            entity_name="Brad",
            roles=["ops", "executor"],
            capabilities=["file-management"],
            poll_interval_seconds=2.0,
        )
        assert cfg.entity_id == "agent://brad"
        assert cfg.roles == ["ops", "executor"]
        assert cfg.poll_interval_seconds == 2.0

    def test_extra_forbid(self) -> None:
        """Typos in config keys should raise ValidationError."""
        with pytest.raises(ValidationError):
            MessagingConfig(unknwon_key="oops")  # type: ignore[call-arg]

    def test_team_root_not_in_module_config(self) -> None:
        """team_root belongs at agent level, not in module config."""
        with pytest.raises(ValidationError):
            MessagingConfig(team_root="./team")  # type: ignore[call-arg]

    def test_roster_ttl_seconds_default(self) -> None:
        """R6.2: Default roster TTL is 60 seconds."""
        cfg = MessagingConfig()
        assert cfg.roster_ttl_seconds == 60.0

    def test_roster_ttl_seconds_custom(self) -> None:
        """R6.3: roster_ttl_seconds configurable."""
        cfg = MessagingConfig(roster_ttl_seconds=30.0)
        assert cfg.roster_ttl_seconds == 30.0
