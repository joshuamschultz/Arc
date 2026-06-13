"""Slack adapter configuration — owned by the extension, not the gateway."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class SlackPlatformConfig(BaseModel):
    """``[platforms.slack]`` schema.

    Attributes:
        enabled: Whether the adapter is active.
        bot_token_env: Env var holding the ``xoxb-`` bot token.
        app_token_env: Env var holding the ``xapp-`` app-level token (Socket Mode).
        allowed_user_ids: Slack user IDs allowed to talk to the agent. Empty
            list = deny all (fail-closed).
        agent_did: Overrides ``[gateway].agent_did`` for this platform.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    bot_token_env: str = "SLACK_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
    app_token_env: str = "SLACK_APP_TOKEN"  # noqa: S105 — env var name, not a secret
    allowed_user_ids: list[str] = Field(default_factory=list)
    agent_did: str = ""

    def resolve_bot_token(self) -> str | None:
        """Read the bot token from the configured env var (None if unset)."""
        return os.environ.get(self.bot_token_env)

    def resolve_app_token(self) -> str | None:
        """Read the app-level token from the configured env var (None if unset)."""
        return os.environ.get(self.app_token_env)
