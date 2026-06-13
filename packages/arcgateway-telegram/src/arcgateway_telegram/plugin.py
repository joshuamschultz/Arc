"""Plugin registration — binds the Telegram adapter into the gateway registry.

The gateway discovers ``PLUGIN`` via the ``arcgateway.adapters`` entry point
(declared in pyproject.toml) and calls :func:`build` for an enabled
``[platforms.telegram]`` block.
"""

from __future__ import annotations

from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterPlugin,
    AdapterUnavailableError,
)

from arcgateway_telegram.adapter import TelegramAdapter
from arcgateway_telegram.config import TelegramPlatformConfig


def build(ctx: AdapterBuildContext) -> TelegramAdapter:
    """Validate config, resolve the token, and construct the adapter.

    Raises:
        AdapterUnavailableError: When the bot token env var is unset. The registry
            skips the adapter at personal/enterprise tier and fails closed at
            federal tier (credential-presence gating).
    """
    cfg = TelegramPlatformConfig.model_validate(ctx.raw_config)
    token = cfg.resolve_token()
    if token is None:
        msg = f"Telegram bot token not found in env var {cfg.token_env!r}"
        raise AdapterUnavailableError(msg)
    return TelegramAdapter(
        bot_token=token,
        allowed_user_ids=cfg.allowed_user_ids,
        on_message=ctx.on_message,
        agent_did=ctx.agent_did(),
    )


PLUGIN = AdapterPlugin(name="telegram", build=build)
