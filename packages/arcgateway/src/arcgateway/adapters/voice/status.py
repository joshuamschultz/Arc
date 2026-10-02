"""Live voice status — what the operator card shows (item 13).

Three parts must all be up for "Olivia" to answer: the gateway adapter, the mic
client (``arc-voice`` on the box that owns the microphone), and the STT/TTS
engine. Each part reports a heartbeat and, when it is down, a plain reason.

Privacy: this model carries lengths, states and timestamps only. The one
exception is ``test_heard``, which is filled only while an operator-started
30-second test is running (LLM02).
"""

from __future__ import annotations

from pydantic import BaseModel

#: A client that has not reported for this long is treated as gone.
STALE_AFTER_SECONDS = 15.0
#: Client states the server accepts; anything else is dropped.
CLIENT_STATES = frozenset({"idle", "listening", "heard", "speaking", "paused"})
CLIENT_MODES = frozenset({"stt-wake", "model", "ptt"})


class PartStatus(BaseModel):
    """One of the three parts: up or down, last heartbeat, plain reason if down."""

    up: bool
    heartbeat_at: float | None = None
    reason: str = ""


class EngineStatus(PartStatus):
    stt: str | None = None
    tts: str | None = None
    last_error: str = ""


class VoiceLiveStatus(BaseModel):
    """Everything the card needs in one read."""

    listening: bool
    state: str  # offline | paused | listening | heard | speaking
    client_connected: bool
    reason: str
    mode: str = ""
    mic: str = ""
    wake_words: list[str]
    wake_mode: str
    wake_match: str = "fuzzy"
    wake_loaded: bool = False
    last_wake_at: float | None = None
    last_seen: float | None = None
    adapter: PartStatus
    client: PartStatus
    engine: EngineStatus
    test_heard: str | None = None


__all__ = [
    "CLIENT_MODES",
    "CLIENT_STATES",
    "STALE_AFTER_SECONDS",
    "EngineStatus",
    "PartStatus",
    "VoiceLiveStatus",
]
