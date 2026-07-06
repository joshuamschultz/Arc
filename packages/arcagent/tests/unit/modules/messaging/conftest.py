"""Shared fixtures for messaging module tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from arcteam.types import Entity


def make_peer_entity(handle: str, name: str | None = None, roles: list[str] | None = None) -> Entity:
    """Build a DID-keyed peer Entity for messaging tests.

    Every entity now requires a real DID + unique handle (REQ-001), so tests
    mint an arctrust identity per peer and key the entity on it.
    """
    from arcteam.types import Entity, EntityType
    from arctrust import AgentIdentity

    identity = AgentIdentity.generate(org="local", agent_type="agent")
    return Entity(
        did=identity.did,
        handle=handle,
        id=f"agent://{handle}",
        name=name or handle.title(),
        type=EntityType.AGENT,
        public_key=identity.public_key.hex(),
        roles=roles or [],
    )


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
