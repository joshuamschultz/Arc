"""Mattermost platform adapter — an in-tree folder, not a distribution.

Air-gapped DOE/National Lab chat surface (FedRAMP High / IL5 / JWICS). Enable
``[platforms.mattermost]`` in ``gateway.toml`` and the registry finds this
folder by its ``PLATFORM`` descriptor (SPEC-065 REQ-308).

Needs ``aiohttp`` — ``pip install 'arcgateway[mattermost]'``.
"""

from arcgateway.adapters.mattermost.adapter import MattermostAdapter
from arcgateway.adapters.mattermost.config import MattermostPlatformConfig
from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterSpec,
    AdapterUnavailableError,
)


def build(ctx: AdapterBuildContext) -> MattermostAdapter:
    """Validate config, resolve the PAT, and construct the adapter.

    The federal-tier air-gap guard runs inside ``MattermostAdapter.__init__``:
    a public-DNS ``server_url`` at federal tier raises ``ValueError``, which the
    registry lets propagate as a hard startup failure (a misconfigured federal
    deployment must refuse to start rather than phone home).

    Raises:
        AdapterUnavailableError: When ``server_url`` is empty or the PAT env var
            is unset.
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
        agent_did=ctx.agent_did(),
        allowed_channel_ids=cfg.allowed_channel_ids or None,
        bot_user_id=cfg.bot_user_id,
        tier=ctx.tier,
        intranet_domains=cfg.intranet_domains,
    )


PLATFORM = AdapterSpec(
    name="mattermost",
    requires=("aiohttp",),
    # Text only, deliberately: this adapter posts through /api/v4/posts and does
    # not upload. Declaring that is what lets the gateway degrade an outbound
    # artefact to a named line *before* it tries, rather than after a failure.
    # Read off the adapter, which is what actually gates delivery.
    supports=MattermostAdapter.supports,
    build=build,
)

__all__ = [
    "PLATFORM",
    "MattermostAdapter",
    "MattermostPlatformConfig",
    "build",
]
