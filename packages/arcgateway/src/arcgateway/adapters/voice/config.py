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

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: A typed wake word: lowercase letters, apostrophes and spaces. No digits or
#: punctuation, so nothing typed here can mangle an address, a path or a log line.
_WAKE_WORD = re.compile(r"^[a-z' ]{2,32}$")
MAX_WAKE_WORDS = 3


class WakeConfig(BaseModel):
    """``[platforms.voice.wake]`` — what the mic box listens for.

    ``stt`` mode matches the typed words in a local transcript (any word, no
    training). ``model`` uses a trained openWakeWord file at ``model_path``.
    Empty ``words`` means "use the channel name" (see ``VoiceAdapter``).
    """

    model_config = ConfigDict(extra="forbid")

    words: list[str] = Field(default_factory=list)
    mode: Literal["stt", "model", "ptt"] = "stt"
    model_path: str = ""
    match: Literal["exact", "fuzzy"] = "fuzzy"

    @field_validator("words")
    @classmethod
    def _words_are_plain(cls, value: list[str]) -> list[str]:
        cleaned = [w.strip().lower() for w in value]
        if len(cleaned) > MAX_WAKE_WORDS:
            raise ValueError(f"at most {MAX_WAKE_WORDS} wake words")
        for word in cleaned:
            if not _WAKE_WORD.fullmatch(word) or not word.replace("'", "").strip():
                raise ValueError(
                    "a wake word is 2-32 characters: lowercase letters, apostrophes and spaces"
                )
        return cleaned


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
    #: Desired listening state; persisted so a restart keeps the operator's choice.
    listening: bool = True
    wake: WakeConfig = Field(default_factory=WakeConfig)
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


__all__ = ["MAX_WAKE_WORDS", "VoicePlatformConfig", "WakeConfig"]
