"""Voice platform adapter — "hey Olivia" at the desk (SPEC-077).

An in-tree folder like every platform: the registry finds it by its ``PLATFORM``
descriptor, and deleting the folder deletes the channel (SPEC-065 REQ-308).

Federal-forbidden (REQ-016): ``build`` refuses at the federal tier, and voice is
deliberately absent from ``registry.OFFICIAL_ADAPTERS`` so the registry also
blocks it there — a desk microphone is out of the SCIF threat model. Defense in
depth: either gate alone stops it.

The heavy machinery (WebRTC transport, the cascade STT/TTS engine, the voice-UX
modules) is wired lazily inside the adapter in later phases; importing this
folder must stay cheap and dependency-free.
"""

from __future__ import annotations

from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterSpec,
    AdapterUnavailableError,
)
from arcgateway.adapters.voice.adapter import VoiceAdapter
from arcgateway.adapters.voice.config import VoicePlatformConfig


def build(ctx: AdapterBuildContext) -> VoiceAdapter:
    """Refuse at federal; otherwise construct the voice adapter for this agent.

    Raises:
        AdapterUnavailableError: At the federal tier (REQ-016). The registry
            skips a raised adapter at personal/enterprise and fails closed at
            federal — but voice never reaches a federal build, because it is not
            an official adapter, so this is the inner of two gates.
    """
    if ctx.tier == "federal":
        raise AdapterUnavailableError(
            "voice channel is forbidden at the federal tier — a desk microphone "
            "is out of the SCIF threat model (SPEC-077 REQ-016)"
        )
    VoicePlatformConfig.model_validate(ctx.raw_config)
    return VoiceAdapter(on_message=ctx.on_message, agent_did=ctx.agent_did())


PLATFORM = AdapterSpec(
    name="voice",
    requires=(),  # Phase 1 skeleton; Phase 2 declares aiortc / openwakeword / etc.
    supports=VoiceAdapter.supports,
    build=build,
)

__all__ = ["PLATFORM", "VoiceAdapter", "VoicePlatformConfig", "build"]
