"""Configuration for the messaging module.

Owned by the messaging module — not part of core config.
Loaded from ``[modules.messaging.config]`` in arcagent.toml.
Validated internally by the module on construction.

NOTE: ``team_root`` is NOT here. It lives at the agent level
in ``[team] root`` so all team modules share one setting.
"""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class MessagingConfig(ModuleConfig):
    """Messaging module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    enabled: bool = False

    # Entity identity — how this agent appears in the registry.
    entity_id: str = ""
    entity_name: str = ""

    # NATS JetStream url for the shared, push-capable substrate (REQ-020/021).
    # Empty selects the dependency-free in-memory backend (local/dev/test).
    nats_url: str = ""

    # Auto-ack messages after the agent reads them via tool.
    auto_ack: bool = True

    # Maximum messages per poll cycle per stream.
    max_messages_per_poll: int = 20

    # Team roster cache TTL in seconds.
    roster_ttl_seconds: float = 60.0
