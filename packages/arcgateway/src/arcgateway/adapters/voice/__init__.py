"""Voice platform adapter — "hey Olivia" at the desk (SPEC-077).

An in-tree folder like every platform: the registry finds it by ``PLATFORM`` and
deleting the folder deletes the channel (SPEC-065 REQ-308).

Federal-forbidden (REQ-016): ``build`` refuses at the federal tier, and voice is
absent from ``registry.OFFICIAL_ADAPTERS`` so the registry blocks it there too.

The pairing token is a credential read from an env var, never inline config (LLM07).
Absent a token the adapter still loads but refuses every connection (fail-closed);
absent an engine selection it runs the fake engine, so it loads without models.
The engine is chosen by config name and resolved through the voice-engine registry
(swap Kokoro/Piper/Whisper/… by config) — see docs/runbooks/voice-channel-deploy.md.
"""

from __future__ import annotations

import os
from typing import Any

from arcgateway.adapters.registry import (
    AdapterBuildContext,
    AdapterSpec,
    AdapterUnavailableError,
)
from arcgateway.adapters.voice.adapter import VoiceAdapter
from arcgateway.adapters.voice.config import VoicePlatformConfig
from arcgateway.adapters.voice.engine import (
    FakeVoiceEngine,
    VoiceEngine,
    build_stt,
    build_tts,
)
from arcgateway.adapters.voice.pairing import VoicePairing
from arcgateway.audit import emit_event


def _build_engine(engine_cfg: dict[str, Any]) -> VoiceEngine:
    """Resolve the cascade from config via the registry; fake when unconfigured."""
    tts_name = engine_cfg.get("tts")
    stt_name = engine_cfg.get("stt")
    if not tts_name and not stt_name:
        return FakeVoiceEngine()
    from arcgateway.adapters.voice.engine.cascade import CascadeEngine

    stt = build_stt(str(stt_name), engine_cfg.get(str(stt_name), {})) if stt_name else None
    tts = build_tts(str(tts_name), engine_cfg.get(str(tts_name), {})) if tts_name else None
    return CascadeEngine(stt=stt, tts=tts)


def build(ctx: AdapterBuildContext) -> VoiceAdapter:
    """Refuse at federal; otherwise construct the voice adapter for this agent."""
    if ctx.tier == "federal":
        raise AdapterUnavailableError(
            "voice channel is forbidden at the federal tier — a desk microphone "
            "is out of the SCIF threat model (SPEC-077 REQ-016)"
        )
    cfg = VoicePlatformConfig.model_validate(ctx.raw_config)
    token = os.environ.get(cfg.token_env)
    pairing = VoicePairing(token=token, operator_did=cfg.operator_did, chat_id=cfg.chat_id)
    if not pairing.is_configured():
        emit_event(
            "voice.pairing.unconfigured",
            f"voice:{ctx.agent_did()}",
            "warn",
            tier=ctx.tier,
            extra={"token_env": cfg.token_env},
        )
    return VoiceAdapter(
        on_message=ctx.on_message,
        agent_did=ctx.agent_did(),
        engine=_build_engine(cfg.engine),
        host=cfg.host,
        port=cfg.port,
        authenticate=pairing.authenticate,
        tier=ctx.tier,
    )


PLATFORM = AdapterSpec(
    name="voice",
    requires=("websockets",),  # v1 transport; models pulled per-box via [voice] extra
    supports=VoiceAdapter.supports,
    build=build,
)

__all__ = ["PLATFORM", "VoiceAdapter", "VoicePlatformConfig", "build"]
