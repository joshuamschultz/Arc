"""Voice platform config (SPEC-077 COMP-001).

The ``[platforms.voice]`` block: which agent, where the WebSocket engine listens,
the pairing-token env var (a credential, never inline), and the engine selection.

``engine`` is config-only and registry-driven: ``engine.tts`` / ``engine.stt``
name a registered engine, and ``engine.<name>`` is that engine's own config
sub-table (models, voices, blend, speed, …), which the engine validates itself.
Absent an engine, the adapter runs the fake engine so it loads without models.

Example (TOML):

    [platforms.voice.engine]
    tts = "kokoro"
    stt = "whisper"
    [platforms.voice.engine.kokoro]
    model_path = "/…/kokoro-v1.0.onnx"
    voices_path = "/…/voices-v1.0.bin"
    blend = "af_jessica:0.6,af_nicole:0.4"
    speed = 1.12
    [platforms.voice.engine.whisper]
    model = "tiny"
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    #: Engine selection + per-engine config sub-tables (registry-resolved by name).
    engine: dict[str, Any] = Field(default_factory=dict)

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


__all__ = ["VoicePlatformConfig"]
