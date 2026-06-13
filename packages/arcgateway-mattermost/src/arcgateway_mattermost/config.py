"""Mattermost adapter configuration — owned by the extension, not the gateway.

Mattermost is the air-gapped DOE/National Lab chat surface (FedRAMP High /
IL5 / JWICS). The federal-tier air-gap guard lives in the adapter itself; this
schema just carries the operator's settings.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class MattermostPlatformConfig(BaseModel):
    """``[platforms.mattermost]`` schema.

    Attributes:
        enabled: Whether the adapter is active.
        server_url: Base HTTPS URL of the Mattermost server (no trailing slash).
        bot_token_env: Env var holding the Personal Access Token (PAT).
        allowed_channel_ids: Channel IDs the bot accepts messages from. Empty
            list = DMs only (conservative default).
        bot_user_id: Mattermost user ID of the bot; used to skip own posts.
        intranet_domains: Hostnames treated as private for the federal-tier
            air-gap guard even if they don't resolve to RFC 1918 addresses.
        agent_did: Overrides ``[gateway].agent_did`` for this platform.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    server_url: str = ""
    bot_token_env: str = "MM_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
    allowed_channel_ids: list[str] = Field(default_factory=list)
    bot_user_id: str = ""
    intranet_domains: list[str] = Field(default_factory=list)
    agent_did: str = ""

    def resolve_bot_token(self) -> str | None:
        """Read the PAT from the configured env var (None if unset)."""
        return os.environ.get(self.bot_token_env)
