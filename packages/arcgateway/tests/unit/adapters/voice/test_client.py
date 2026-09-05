"""T-019 — the desk client orchestration (SPEC-077 COMP-002, REQ-003).

Capture -> send -> playback, tested with a fake client and injected audio io so no
microphone is needed. The real capture/playback use sounddevice on the Mac.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np

from arcgateway.adapters.voice.client import VoiceDeskClient


class _FakeClient:
    def __init__(self, reply: bytes | None) -> None:
        self.reply = reply
        self.sent: list[bytes] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send_utterance(self, pcm: bytes) -> bytes | None:
        self.sent.append(pcm)
        return self.reply

    async def close(self) -> None:
        self.closed = True


async def test_run_once_sends_the_utterance_and_plays_the_reply() -> None:
    fake = _FakeClient(reply=b"WAV-reply")
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    played: list[bytes] = []

    async def capture() -> bytes:
        return b"utterance-pcm"

    async def playback(wav: bytes) -> None:
        played.append(wav)

    assert await desk.run_once(capture=capture, playback=playback) is True
    assert fake.sent == [b"utterance-pcm"]
    assert played == [b"WAV-reply"]


async def test_run_once_with_no_audio_sends_nothing() -> None:
    fake = _FakeClient(reply=None)
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]

    async def capture() -> bytes:
        return b""

    async def playback(_wav: bytes) -> None:  # pragma: no cover
        raise AssertionError("nothing to play when capture is empty")

    assert await desk.run_once(capture=capture, playback=playback) is False
    assert fake.sent == []


async def test_run_loop_connects_and_closes() -> None:
    fake = _FakeClient(reply=None)
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    calls = {"n": 0}

    async def capture() -> bytes:
        calls["n"] += 1
        if calls["n"] > 2:
            raise KeyboardInterrupt
        return b""

    async def playback(_wav: bytes) -> None:  # pragma: no cover
        return None

    try:
        await desk.run_loop(capture=capture, playback=playback)
    except KeyboardInterrupt:
        pass
    assert fake.connected is True
    assert fake.closed is True


async def test_stt_wake_sends_only_the_segment_with_the_wake_word() -> None:
    fake = _FakeClient(reply=b"WAV-reply")
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    played: list[bytes] = []

    loud = (np.ones(1280, dtype=np.int16) * 3000).tobytes()
    quiet = np.zeros(1280, dtype=np.int16).tobytes()
    seq = [loud] * 3 + [quiet] * 15 + [loud] * 3 + [quiet] * 15  # two speech segments

    async def frames() -> AsyncIterator[bytes]:
        for frame in seq:
            yield frame

    calls = {"n": 0}

    async def transcribe(_pcm: bytes) -> str:
        calls["n"] += 1
        return "hey olivia what time is it" if calls["n"] == 1 else "talking to myself"

    async def playback(wav: bytes) -> None:
        played.append(wav)

    await desk.run_stt_wake(
        frames=frames(), transcribe=transcribe, playback=playback, silence_frames=15
    )

    assert calls["n"] == 2  # both segments transcribed locally
    assert len(fake.sent) == 1  # only the "olivia" one reached the gateway
    assert played == [b"WAV-reply"]
