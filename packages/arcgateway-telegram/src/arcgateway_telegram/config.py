"""Telegram adapter configuration — owned by the extension, not the gateway.

The ``[platforms.telegram]`` block in ``gateway.toml`` is handed to this plugin
as a raw dict and validated here. The gateway core never sees these fields.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class TelegramPlatformConfig(BaseModel):
    """``[platforms.telegram]`` schema.

    Attributes:
        enabled: Whether the adapter is active (the registry checks this too).
        token_env: Name of the env var holding the bot token. The value is read
            at runtime — the token is never stored in the config file.
        allowed_user_ids: Telegram user IDs allowed to talk to the agent. Empty
            list = deny all (fail-closed).
        agent_did: Overrides ``[gateway].agent_did`` for this platform.
    """

    # Tolerate sibling keys (enabled/agent_did are declared; anything else the
    # operator adds is ignored rather than rejected).
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
    allowed_user_ids: list[int] = Field(default_factory=list)
    agent_did: str = ""

    def resolve_token(self) -> str | None:
        """Read the bot token from the configured env var (None if unset)."""
        return os.environ.get(self.token_env)
