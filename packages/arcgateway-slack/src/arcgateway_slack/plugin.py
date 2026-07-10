"""Plugin registration — binds the Slack adapter into the gateway registry."""

from __future__ import annotations

from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterPlugin,
    AdapterUnavailableError,
)

from arcgateway_slack.adapter import SlackAdapter
from arcgateway_slack.config import SlackPlatformConfig


def build(ctx: AdapterBuildContext) -> SlackAdapter:
    """Validate config, resolve both tokens, and construct the adapter.

    Raises:
        AdapterUnavailableError: When the bot or app token env var is unset.
    """
    cfg = SlackPlatformConfig.model_validate(ctx.raw_config)
    bot_token = cfg.resolve_bot_token()
    app_token = cfg.resolve_app_token()
    if bot_token is None or app_token is None:
        missing = cfg.bot_token_env if bot_token is None else cfg.app_token_env
        msg = f"Slack token not found in env var {missing!r}"
        raise AdapterUnavailableError(msg)
    return SlackAdapter(
        bot_token=bot_token,
        app_token=app_token,
        allowed_user_ids=cfg.allowed_user_ids,
        on_message=ctx.on_message,
        agent_did=ctx.agent_did(),
        require_pairing=ctx.require_pairing,
    )


PLUGIN = AdapterPlugin(name="slack", build=build)
