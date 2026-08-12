"""Telegram platform adapter — an in-tree folder, not a distribution.

Enable ``[platforms.telegram]`` in ``gateway.toml`` and the registry finds this
folder by its ``PLATFORM`` descriptor (SPEC-065 REQ-308). Deleting the folder
deletes the platform; nothing else is edited either way.

Needs ``python-telegram-bot`` — ``pip install 'arcgateway[telegram]'``. Without
it the platform is skipped with that reason at personal/enterprise tier and
refuses startup at federal.
"""

from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterSpec,
    AdapterUnavailableError,
)
from arcgateway.adapters.telegram.adapter import TelegramAdapter
from arcgateway.adapters.telegram.config import TelegramPlatformConfig


def build(ctx: AdapterBuildContext) -> TelegramAdapter:
    """Validate config, resolve the token, and construct the adapter.

    Raises:
        AdapterUnavailableError: When the bot token env var is unset. The
            registry skips the adapter at personal/enterprise tier and fails
            closed at federal tier (credential-presence gating).
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
        require_pairing=ctx.require_pairing,
    )


PLATFORM = AdapterSpec(
    name="telegram",
    requires=("python-telegram-bot",),
    # Read off the adapter, which is what actually gates delivery — one source,
    # so the contract suite and `send` can never disagree about a kind.
    supports=TelegramAdapter.supports,
    build=build,
)

__all__ = [
    "PLATFORM",
    "TelegramAdapter",
    "TelegramPlatformConfig",
    "build",
]
