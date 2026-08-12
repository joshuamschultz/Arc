"""Slack (Socket Mode) platform adapter — an in-tree folder, not a distribution.

Enable ``[platforms.slack]`` in ``gateway.toml`` and the registry finds this
folder by its ``PLATFORM`` descriptor (SPEC-065 REQ-308).

Needs ``slack-bolt`` — ``pip install 'arcgateway[slack]'``.
"""

from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterSpec,
    AdapterUnavailableError,
)
from arcgateway.adapters.slack.adapter import SlackAdapter
from arcgateway.adapters.slack.config import SlackPlatformConfig


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


PLATFORM = AdapterSpec(
    name="slack",
    requires=("slack-bolt", "slack-sdk", "aiohttp"),
    # No "audio": Slack has no audio-message surface, so an audio part degrades
    # to a description rather than being uploaded as an opaque file.
    # Read off the adapter, which is what actually gates delivery.
    supports=SlackAdapter.supports,
    build=build,
)

__all__ = [
    "PLATFORM",
    "SlackAdapter",
    "SlackPlatformConfig",
    "build",
]
