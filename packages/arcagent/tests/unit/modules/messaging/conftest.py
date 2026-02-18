"""Shared fixtures for messaging module tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock


def make_config_dict(
    entity_id: str = "agent://test_agent",
    entity_name: str = "Test Agent",
    roles: list[str] | None = None,
    capabilities: list[str] | None = None,
    poll_interval_seconds: float = 0.1,
    auto_ack: bool = True,
) -> dict[str, Any]:
    """Build a config dict for MessagingConfig."""
    return {
        "enabled": True,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "roles": roles or ["executor"],
        "capabilities": capabilities or ["task-execution"],
        "poll_interval_seconds": poll_interval_seconds,
        "auto_register": True,
        "auto_ack": auto_ack,
        "audit_hmac_key": "test-key",
        "max_messages_per_poll": 20,
    }


def make_team_config(team_root: str) -> MagicMock:
    """Create a mock TeamSection with the given root."""
    tc = MagicMock()
    tc.root = team_root
    return tc


def make_ctx(tmp_path: Path) -> MagicMock:
    """Create a mock ModuleContext for startup tests."""
    ctx = MagicMock()
    ctx.bus = MagicMock()
    ctx.bus.subscribe = MagicMock()
    ctx.tool_registry = MagicMock()
    ctx.tool_registry.register = MagicMock()
    ctx.workspace = tmp_path
    ctx.config = MagicMock()
    ctx.config.agent.name = "test_agent"
    return ctx
