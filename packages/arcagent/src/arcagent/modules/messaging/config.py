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

    # Entity identity, continued: the agent's role in one line, published on the
    # roster so a teammate can address the right agent by name.
    entity_role: str = ""

    # ADR-032: route an un-addressed channel post by ranking every agent's
    # published digest, so only the agents that hold something relevant pay a
    # full turn. @mentions and critical priority never reach the router.
    channel_route: bool = True

    # How many agents a single un-addressed post may wake. Two is the shape the
    # owner described: the agent who owns it answers, and one other with
    # genuinely different context adds to it.
    route_top_k: int = 2

    # How close the top two must be before the ranking is handed to the router.
    # A relative gap, so it is scale-free across question lengths.
    route_ambiguity_margin: float = 0.25

    # The tiebreak needs a deadline of its own: without one a hung provider
    # blocks this agent's inbox consumer outright.
    route_timeout_seconds: float = 5.0

    # Per-(agent, channel) breaker around the routing pass. A router that keeps
    # failing stops being called at all, with exponential backoff, instead of
    # being retried on every message forever.
    route_failure_threshold: int = 5
    route_base_wait_seconds: float = 30.0

    # The dense half of the hybrid ranker. Empty means lexical-only, which is
    # deliberate as the default: BM25 is the half that finds a rare identifier
    # like "NNL", and an embedder that downloads a model the first time somebody
    # speaks is not an unbreakable default. Set to "local" or "provider" to add
    # recall on questions that paraphrase rather than name.
    route_embed_backend: str = ""
    route_embed_model: str = ""
    route_embed_base_url: str = ""

    # SPEC-068 D1c — blast radius for ONE un-addressed post. The cap is soft
    # (members decide independently, with no coordinator) and both are bypassed
    # by an @mention, which must never be silenced by someone answering first.
    channel_answer_cap: int = 2
    channel_cooldown_seconds: float = 60.0
