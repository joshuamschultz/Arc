"""Voice platform config (SPEC-077 COMP-001).

The ``[platforms.voice]`` block: which agent, where the WebSocket engine listens,
the pairing-token env var (a credential, never inline), and the cascade engine's
model settings. Absent an engine voice model, the adapter falls back to the fake
engine so it loads and pairs even without models installed (dev / CI).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VoiceEngineConfig(BaseModel):
    """Cascade model settings (see docs/runbooks/voice-channel-deploy.md)."""

    model_config = ConfigDict(extra="ignore")

    stt_model: str = "tiny"
    stt_device: str = "cpu"
    stt_compute: str = "int8"
    stt_model_path: str | None = None
    #: Path to a Piper ``.onnx`` voice. None -> fake engine (no models needed).
    tts_voice: str | None = None


class VoicePlatformConfig(BaseModel):
    """Validated shape of the ``[platforms.voice]`` TOML block."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    agent_did: str | None = None
    host: str = "127.0.0.1"
    port: int = 8790
    #: Env var holding the pairing token (never the token itself — LLM07).
    token_env: str = "ARC_VOICE_TOKEN"  # noqa: S105 - env var NAME, not a secret value
    operator_did: str = "did:arc:operator"
    chat_id: str = "voice"
    engine: VoiceEngineConfig = Field(default_factory=VoiceEngineConfig)

    @field_validator("chat_id")
    @classmethod
    def _chat_id_has_no_colon(cls, value: str) -> str:
        # The agent's reply address is "platform:chat_id[:thread_id]", split on
        # ":" — a colon in chat_id mangles the reply target so the spoken answer
        # never routes back. Reject it loudly rather than fail silently.
        if ":" in value:
            raise ValueError(
                "chat_id must not contain ':' — it collides with the "
                "'platform:chat_id:thread' delivery address"
            )
        return value


__all__ = ["VoiceEngineConfig", "VoicePlatformConfig"]
