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

    # Entity identity, continued: the agent's role in one line. The relevance
    # gate asks "is this about your role?", which is unanswerable without it.
    entity_role: str = ""

    # SPEC-055/068: gate a channel broadcast (no @mentions) behind a cheap
    # per-agent relevance check before the full run, so only agents the message
    # concerns pay a full turn. @mentions and critical priority bypass the gate.
    channel_triage: bool = True

    # SPEC-068 D1a — the gate FAILS CLOSED, so it needs a deadline of its own:
    # without one a hung provider blocks this agent's inbox consumer outright.
    # Small because the gate answers one word and must stay a rounding error
    # against the turn it is deciding about.
    triage_timeout_seconds: float = 5.0

    # SPEC-068 D4d — per-(agent, channel) breaker around the gate. A gate that
    # keeps failing stops being called at all, with exponential backoff, instead
    # of being retried on every message forever.
    triage_failure_threshold: int = 5
    triage_base_wait_seconds: float = 30.0

    # SPEC-068 D1c — blast radius for ONE un-addressed post. The cap is soft
    # (members decide independently, with no coordinator) and both are bypassed
    # by an @mention, which must never be silenced by someone answering first.
    channel_answer_cap: int = 2
    channel_cooldown_seconds: float = 60.0
