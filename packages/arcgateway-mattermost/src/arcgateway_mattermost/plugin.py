"""Plugin registration — binds the Mattermost adapter into the gateway registry."""

from __future__ import annotations

from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterPlugin,
    AdapterUnavailableError,
)

from arcgateway_mattermost.adapter import MattermostAdapter
from arcgateway_mattermost.config import MattermostPlatformConfig


def build(ctx: AdapterBuildContext) -> MattermostAdapter:
    """Validate config, resolve the PAT, and construct the adapter.

    The federal-tier air-gap guard runs inside ``MattermostAdapter.__init__``:
    a public-DNS ``server_url`` at federal tier raises ``ValueError``, which the
    registry lets propagate as a hard startup failure (a misconfigured federal
    deployment must refuse to start rather than phone home).

    Raises:
        AdapterUnavailableError: When ``server_url`` is empty or the PAT env var is unset.
    """
    cfg = MattermostPlatformConfig.model_validate(ctx.raw_config)
    if not cfg.server_url:
        raise AdapterUnavailableError("Mattermost server_url is empty")
    token = cfg.resolve_bot_token()
    if token is None:
        msg = f"Mattermost token not found in env var {cfg.bot_token_env!r}"
        raise AdapterUnavailableError(msg)
    return MattermostAdapter(
        server_url=cfg.server_url,
        bot_token=token,
        on_message=ctx.on_message,
        allowed_channel_ids=cfg.allowed_channel_ids or None,
        bot_user_id=cfg.bot_user_id,
        tier=ctx.tier,
        intranet_domains=cfg.intranet_domains,
    )


PLUGIN = AdapterPlugin(name="mattermost", build=build)
