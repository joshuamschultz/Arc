"""Configuration for the messaging module.

Owned by the messaging module — not part of core config.
Loaded from ``[modules.messaging.config]`` in arcagent.toml.
Validated internally by the module on construction.

NOTE: ``team_root`` is NOT here. It lives at the agent level
in ``[team] root`` so all team modules share one setting.
"""

from __future__ import annotations

from pydantic import Field

from arcagent.modules.base_config import ModuleConfig


class MessagingConfig(ModuleConfig):
    """Messaging module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    enabled: bool = False

    # Entity identity — how this agent appears in the registry.
    entity_id: str = ""
    entity_name: str = ""
    roles: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)

    # Auto-register this agent in the entity registry on startup.
    auto_register: bool = True

    # Polling configuration.
    poll_interval_seconds: float = 5.0

    # Auto-ack messages after the agent reads them via tool.
    auto_ack: bool = True

    # HMAC key for audit chain integrity.
    # In production: pulled from vault. For local dev: a static key.
    audit_hmac_key: str = "arcteam-local-dev"

    # Maximum messages per poll cycle per stream.
    max_messages_per_poll: int = 20

    # Team roster cache TTL in seconds.
    roster_ttl_seconds: float = 60.0
