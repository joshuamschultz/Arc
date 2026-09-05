"""Voice desk client — the thin mic/speaker end (SPEC-077 COMP-002, REQ-003/011).

Runs on the desk box (Mac). Push-to-talk: press Enter, speak, press Enter — the
utterance goes to the engine host over the WebSocket, and the spoken reply plays
back. Capture and playback are injected so the orchestration is testable without a
microphone; the real ones use ``sounddevice`` (lazy import, optional [voice] dep).

The wake word ("hey Olivia" via openWakeWord) and WebRTC/AEC barge-in are the
follow-up; push-to-talk needs neither and is enough to talk today.
"""

from __future__ import annotations

import asyncio
import io
import os
import wave
from collections.abc import Awaitable, Callable
from typing import Any

from arcgateway.adapters.voice.transport import VoiceClient
from arcgateway.adapters.voice.wake import OpenWakeWordDetector, WakeDetector, WakeGate

Capture = Callable[[], Awaitable[bytes]]
Playback = Callable[[bytes], Awaitable[None]]

_SAMPLE_RATE = 16000


def _collect_utterance(
    frames: Any,
    np: Any,
    *,
    silence_frames: int = 15,  # ~1.2 s of trailing silence at 80 ms/frame
    max_frames: int = 250,  # ~20 s hard cap
    rms_threshold: float = 500.0,
) -> bytes:
    """Drain mic frames after a wake until trailing silence (energy-based)."""
    import queue as _queue

    collected: list[bytes] = []
    silence = 0
    spoke = False
    for _ in range(max_frames):
        try:
            frame = frames.get(timeout=5.0)
        except _queue.Empty:
            break
        collected.append(frame)
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples**2) + 1e-9))
        if rms >= rms_threshold:
            spoke = True
            silence = 0
        elif spoke:
            silence += 1
            if silence >= silence_frames:
                break
    return b"".join(collected) if spoke else b""


class VoiceDeskClient:
    """Orchestrates capture -> send -> playback against a VoiceClient."""

    def __init__(self, client: VoiceClient) -> None:
        self._client = client

    async def run_once(self, *, capture: Capture, playback: Playback) -> bool:
        """One PTT turn. Returns False when there was nothing to send."""
        pcm = await capture()
        if not pcm:
            return False
        wav = await self._client.send_utterance(pcm)
        if wav:
            await playback(wav)
        return True

    async def run_loop(self, *, capture: Capture, playback: Playback) -> None:
        await self._client.connect()
        try:
            while True:
                await self.run_once(capture=capture, playback=playback)
        finally:
            await self._client.close()

    async def run_always_on(
        self,
        *,
        detector: WakeDetector,
        playback: Playback | None = None,
        on_wake: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Always-listening loop: wake word -> capture -> send -> play -> re-arm.

        Streams nothing until the wake word fires (REQ-007). Runs on the box with
        the mic (the DGX's USB mic). Uses sounddevice; needs a real device, so it
        is exercised in the field, not in CI — the wake gate and capture endpoint
        logic it drives are unit-tested separately.
        """
        import queue

        import numpy as np  # lazy
        import sounddevice as sd  # lazy

        play = playback or _default_playback
        gate = WakeGate(detector)
        frames: queue.Queue[bytes] = queue.Queue()

        def _cb(indata: Any, _n: int, _t: Any, _s: Any) -> None:
            frames.put(bytes(indata))

        await self._client.connect()
        stream = sd.InputStream(
            samplerate=_SAMPLE_RATE, channels=1, dtype="int16", blocksize=1280, callback=_cb
        )
        stream.start()
        try:
            while True:
                frame = await asyncio.to_thread(frames.get)
                if not gate.on_frame(frame):
                    continue
                if on_wake is not None:
                    await on_wake()
                pcm = await asyncio.to_thread(_collect_utterance, frames, np)
                gate.reset()
                if not pcm:
                    continue
                wav = await self._client.send_utterance(pcm)
                if wav:
                    await play(wav)
        finally:
            stream.stop()
            stream.close()
            await self._client.close()


def _record_ptt() -> bytes:
    import numpy as np  # lazy: optional [voice] dep
    import sounddevice as sd  # lazy: optional [voice] dep

    input("🎙  Press Enter, speak, then press Enter to send…")
    frames: list[Any] = []
    stream = sd.InputStream(
        samplerate=_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        callback=lambda indata, _f, _t, _s: frames.append(indata.copy()),
    )
    stream.start()
    input("   …recording — press Enter to send.")
    stream.stop()
    stream.close()
    if not frames:
        return b""
    return np.concatenate(frames).tobytes()


def _play_wav(wav: bytes) -> None:
    import numpy as np  # lazy
    import sounddevice as sd  # lazy

    with wave.open(io.BytesIO(wav)) as handle:
        rate = handle.getframerate()
        pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    sd.play(pcm, rate)
    sd.wait()


async def _default_capture() -> bytes:
    return await asyncio.to_thread(_record_ptt)


async def _default_playback(wav: bytes) -> None:
    await asyncio.to_thread(_play_wav, wav)


def main() -> None:
    """Console entry (`arc-voice`). URI + token come from the environment."""
    uri = os.environ.get("ARC_VOICE_URI", "ws://127.0.0.1:8790")
    token = os.environ.get("ARC_VOICE_TOKEN", "")
    if not token:
        raise SystemExit("set ARC_VOICE_TOKEN (the pairing token) to connect")
    wake_model = os.environ.get("ARC_VOICE_WAKE_MODEL")
    desk = VoiceDeskClient(VoiceClient(uri=uri, token=token))
    try:
        if wake_model:
            print(f"Always-on 'hey Olivia' ({wake_model}) → {uri}  (Ctrl-C to quit)")  # noqa: T201
            asyncio.run(desk.run_always_on(detector=OpenWakeWordDetector(model_path=wake_model)))
        else:
            print(f"Push-to-talk → {uri}  (Ctrl-C to quit)")  # noqa: T201 - CLI user output
            asyncio.run(desk.run_loop(capture=_default_capture, playback=_default_playback))
    except KeyboardInterrupt:
        print("\nbye")  # noqa: T201 - CLI user output


__all__ = ["Capture", "Playback", "VoiceDeskClient", "main"]

if __name__ == "__main__":
    main()
