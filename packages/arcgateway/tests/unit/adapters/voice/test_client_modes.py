"""Item 13 — the mic client obeys the gateway: pause closes the mic, words update live."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import numpy as np

from arcgateway.adapters.voice.client import VoiceDeskClient

_LOUD = (np.ones(1280, dtype=np.int16) * 3000).tobytes()
_QUIET = np.zeros(1280, dtype=np.int16).tobytes()
_SEGMENT = [_LOUD, _LOUD, _QUIET, _QUIET]  # silence_frames=2 closes it


class _FakeClient:
    """Stands in for VoiceClient: records frames, lets a test play the gateway."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.frames: list[dict[str, Any]] = []
        self.closed = False
        self.on_send: Any = None
        self.control_handler: Any = lambda _p: None

    async def connect(self) -> None:
        return None

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.frames.append(payload)

    async def send_utterance(self, pcm: bytes) -> bytes | None:
        self.sent.append(pcm)
        if self.on_send is not None:
            self.on_send()
        return None

    async def close(self) -> None:
        self.closed = True


def _frames(segments: int, opened: dict[str, int]) -> AsyncGenerator[bytes, None]:
    async def gen() -> AsyncGenerator[bytes, None]:
        opened["open"] += 1
        try:
            for _ in range(segments):
                for frame in _SEGMENT:
                    yield frame
        finally:
            opened["open"] -= 1
            opened["closed_count"] += 1

    return gen()


async def _play(_wav: bytes) -> None:
    return None


async def test_paused_sends_no_audio_and_closes_mic() -> None:
    fake = _FakeClient()
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    opened = {"open": 0, "closed_count": 0}
    # the gateway pauses us right after the first utterance goes out
    fake.on_send = lambda: fake.control_handler({"t": "cmd", "listening": False})

    async def transcribe(_audio: bytes) -> str:
        return "olivia what time is it"

    task = asyncio.create_task(
        desk.run_stt_wake(
            open_frames=lambda: _frames(3, opened),
            transcribe=transcribe,
            playback=_play,
            silence_frames=2,
        )
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if opened["closed_count"] and any(f.get("state") == "paused" for f in fake.frames):
            break
    assert len(fake.sent) == 1  # nothing after the pause
    assert opened["open"] == 0  # the mic source was closed, not just ignored
    assert fake.frames[-1]["state"] == "paused"

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert fake.closed is True


async def test_resume_reopens_the_mic() -> None:
    fake = _FakeClient()
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    opened = {"open": 0, "closed_count": 0}

    async def transcribe(_audio: bytes) -> str:
        return "olivia hi"

    desk.control.apply({"listening": False})
    task = asyncio.create_task(
        desk.run_stt_wake(
            open_frames=lambda: _frames(1, opened),
            transcribe=transcribe,
            playback=_play,
            silence_frames=2,
        )
    )
    await asyncio.sleep(0.05)
    assert (
        opened["closed_count"] == 0 and fake.sent == []
    )  # paused from the start: mic never opened
    desk.control.apply({"listening": True})
    await asyncio.wait_for(task, 2)  # frames end on their own -> run returns
    assert len(fake.sent) == 1


async def test_wake_words_update_at_runtime() -> None:
    fake = _FakeClient()
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    opened = {"open": 0, "closed_count": 0}
    calls = {"n": 0}

    async def transcribe(_audio: bytes) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            # the operator types a new word while the client is running
            fake.control_handler({"t": "cmd", "wake": {"words": ["computer"], "match": "exact"}})
            return "olivia hello"  # old word: must no longer wake
        return "computer lights on"

    await desk.run_stt_wake(
        open_frames=lambda: _frames(2, opened),
        transcribe=transcribe,
        playback=_play,
        silence_frames=2,
    )
    assert calls["n"] == 2
    assert len(fake.sent) == 1  # only "computer ..." woke it
    assert desk.control.wake_words == ("computer",)


async def test_env_pinned_words_cannot_be_changed_by_the_gateway() -> None:
    fake = _FakeClient()
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    opened = {"open": 0, "closed_count": 0}

    async def transcribe(_audio: bytes) -> str:
        return "olivia hi"

    fake.on_send = None
    fake.control_handler({"t": "cmd", "wake": {"words": ["hijack"]}})

    async def run() -> None:
        await desk.run_stt_wake(
            open_frames=lambda: _frames(1, opened),
            transcribe=transcribe,
            playback=_play,
            wake_words=("olivia",),
            silence_frames=2,
        )

    await run()
    fake.control_handler({"t": "cmd", "wake": {"words": ["hijack"]}})
    assert desk.control.wake_words == ("olivia",)
    assert len(fake.sent) == 1


async def test_transcript_is_reported_only_during_a_test() -> None:
    fake = _FakeClient()
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    opened = {"open": 0, "closed_count": 0}

    async def transcribe(_audio: bytes) -> str:
        return "private chatter"

    await desk.run_stt_wake(
        open_frames=lambda: _frames(1, opened),
        transcribe=transcribe,
        playback=_play,
        silence_frames=2,
    )
    assert all("heard" not in f for f in fake.frames)

    fake2 = _FakeClient()
    desk2 = VoiceDeskClient(fake2)  # type: ignore[arg-type]
    desk2.control.apply({"test": True})
    await desk2.run_stt_wake(
        open_frames=lambda: _frames(1, opened),
        transcribe=transcribe,
        playback=_play,
        silence_frames=2,
    )
    assert any(f.get("heard") == "private chatter" for f in fake2.frames)


async def test_hello_reports_mode_and_mic() -> None:
    fake = _FakeClient()
    desk = VoiceDeskClient(fake)  # type: ignore[arg-type]
    opened = {"open": 0, "closed_count": 0}

    async def transcribe(_audio: bytes) -> str:
        return ""

    await desk.run_stt_wake(
        open_frames=lambda: _frames(1, opened),
        transcribe=transcribe,
        playback=_play,
        mic="plughw:CARD=MV7i,DEV=0",
        silence_frames=2,
    )
    hello = next(f for f in fake.frames if f["t"] == "hello")
    assert hello["mode"] == "stt-wake"
    assert hello["mic"] == "plughw:CARD=MV7i,DEV=0"
