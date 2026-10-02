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
import contextlib
import io
import logging
import os
import time
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from arcgateway.adapters.voice.transport import VoiceClient
from arcgateway.adapters.voice.wake import (
    MatchMode,
    OpenWakeWordDetector,
    WakeDetector,
    WakeGate,
    match_wake,
)

Capture = Callable[[], Awaitable[bytes]]
Playback = Callable[[bytes], Awaitable[None]]

_SAMPLE_RATE = 16000
#: Seconds between state heartbeats to the gateway (it calls us stale at 15 s).
HEARTBEAT_SECONDS = 5.0
_log = logging.getLogger("arcgateway.voice.client")


@dataclass
class ClientControl:
    """What the gateway told this client to do. Server-owned; env can pin the words."""

    listening: bool = True
    wake_words: tuple[str, ...] = ("olivia",)
    match: MatchMode = "fuzzy"
    #: set when ARC_VOICE_WAKE_WORDS pins the words; the gateway then cannot change them.
    words_pinned: bool = False
    test_until: float = 0.0
    resume: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.resume.set()

    def apply(self, payload: dict[str, Any]) -> None:
        """Fold a ``ready`` or ``cmd`` frame in. Unknown or malformed fields are ignored."""
        listening = payload.get("listening")
        if isinstance(listening, bool):
            self.listening = listening
            if listening:
                self.resume.set()
            else:
                self.resume.clear()
        wake = payload.get("wake")
        if isinstance(wake, dict):
            self._apply_wake(wake)
        if payload.get("test") is True:
            self.test_until = time.monotonic() + 30.0

    def _apply_wake(self, wake: dict[str, Any]) -> None:
        words = wake.get("words")
        if not self.words_pinned and isinstance(words, list) and words:
            self.wake_words = tuple(str(w) for w in words if isinstance(w, str))
        match = wake.get("match")
        if match in ("exact", "fuzzy"):
            self.match = match

    def testing(self) -> bool:
        return time.monotonic() < self.test_until


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


def _normalize(pcm: bytes, np: Any, target_peak: float) -> bytes:
    """Peak-normalize a segment toward ``target_peak`` so a quiet mic transcribes."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    peak = float(np.max(np.abs(samples))) or 1.0
    gain = min(20.0, target_peak / peak)  # cap so pure noise isn't blown up
    return bytes(np.clip(samples * gain, -32768, 32767).astype(np.int16).tobytes())


class VoiceDeskClient:
    """Orchestrates capture -> send -> playback against a VoiceClient."""

    def __init__(self, client: VoiceClient) -> None:
        self._client = client
        self.control = ClientControl()
        self._state = "idle"
        self._last_wake_at: float | None = None
        self._heard = ""
        client.control_handler = self.control.apply

    async def _hello(self, mode: str, mic: str) -> None:
        await self._client.send_json(
            {
                "t": "hello",
                "mode": mode,
                "mic": mic or "none",
                "wake": ",".join(self.control.wake_words) if mode == "stt-wake" else "model",
                "wake_loaded": True,
            }
        )

    async def _report(self) -> None:
        frame: dict[str, Any] = {
            "t": "state",
            "state": self._state,
            "last_wake_at": self._last_wake_at,
        }
        if self.control.testing() and self._heard:
            frame["heard"] = self._heard  # only during an operator-started test
        await self._client.send_json(frame)

    async def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            await self._report()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._report()

    async def _session(self, mode: str, mic: str) -> asyncio.Task[None]:
        """Connect, introduce this client, and start the heartbeat."""
        await self._client.connect()
        await self._hello(mode, mic)
        await self._report()
        return asyncio.create_task(self._heartbeat())

    async def _end_session(self, beat: asyncio.Task[None]) -> None:
        beat.cancel()
        await self._client.close()

    async def run_once(self, *, capture: Capture, playback: Playback) -> bool:
        """One PTT turn. Returns False when there was nothing to send."""
        if not self.control.listening:
            return False  # paused: capture nothing, send nothing
        pcm = await capture()
        if not pcm:
            return False
        wav = await self._client.send_utterance(pcm)
        if wav:
            await playback(wav)
        return True

    async def run_loop(self, *, capture: Capture, playback: Playback) -> None:
        beat = await self._session("ptt", "")
        try:
            while True:
                if not self.control.listening:
                    await self._set_state("paused")
                    await self.control.resume.wait()
                    await self._set_state("idle")
                await self.run_once(capture=capture, playback=playback)
        finally:
            await self._end_session(beat)

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
        logic it drives are unit-tested separately. Paused means the input stream
        is stopped, so the microphone is not open.
        """
        import queue

        import numpy as np  # lazy
        import sounddevice as sd  # lazy

        play = playback or _default_playback
        gate = WakeGate(detector)
        frames: queue.Queue[bytes] = queue.Queue()

        def _cb(indata: Any, _n: int, _t: Any, _s: Any) -> None:
            frames.put(bytes(indata))

        beat = await self._session("model", "default")
        stream = sd.InputStream(
            samplerate=_SAMPLE_RATE, channels=1, dtype="int16", blocksize=1280, callback=_cb
        )
        stream.start()
        try:
            while True:
                if not self.control.listening:
                    stream.stop()
                    await self._set_state("paused")
                    await self.control.resume.wait()
                    gate.reset()
                    stream.start()
                    await self._set_state("idle")
                try:
                    frame = await asyncio.to_thread(frames.get, True, 1.0)
                except queue.Empty:
                    continue
                if not gate.on_frame(frame):
                    continue
                self._last_wake_at = time.time()
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
            await self._end_session(beat)

    async def run_stt_wake(
        self,
        *,
        open_frames: Callable[[], AsyncGenerator[bytes, None]],
        transcribe: Callable[[bytes], Awaitable[str]],
        playback: Playback,
        wake_words: tuple[str, ...] | None = None,
        mic: str = "",
        rms_threshold: float = 500.0,
        silence_frames: int = 15,
        target_peak: float = 0.0,
    ) -> None:
        """Always-on without a trained model: local STT gates on the wake phrase.

        Segments speech by energy (dropping pre-speech silence, so nothing is
        acted on until you speak), transcribes each segment LOCALLY, and only when
        the transcript matches a wake word does it send the utterance to the gateway
        and play the reply. The wake words come from the gateway (typed in the card)
        and can change at runtime; ``wake_words`` (env) pins them. Paused means the
        frame source is closed, so the microphone is not open. ``target_peak`` (>0)
        peak-normalizes each segment before STT so a quiet mic still transcribes.
        openWakeWord (``run_always_on``) is the upgrade.
        """
        if wake_words:
            self.control.wake_words = wake_words
            self.control.words_pinned = True
        beat = await self._session("stt-wake", mic)
        _log.info(
            "stt-wake listening (rms>=%.0f, wake=%s, target_peak=%.0f)",
            rms_threshold,
            self.control.wake_words,
            target_peak,
        )
        try:
            while True:
                if not self.control.listening:
                    await self._set_state("paused")
                    await self.control.resume.wait()
                    continue
                await self._set_state("idle")
                frames = open_frames()
                try:
                    await self._segment_loop(
                        frames, transcribe, playback, rms_threshold, silence_frames, target_peak
                    )
                finally:
                    await frames.aclose()
                if self.control.listening:
                    return  # the frame source ended on its own (mic gone)
        finally:
            await self._end_session(beat)

    async def _segment_loop(
        self,
        frames: AsyncIterator[bytes],
        transcribe: Callable[[bytes], Awaitable[str]],
        playback: Playback,
        rms_threshold: float,
        silence_frames: int,
        target_peak: float,
    ) -> None:
        """Segment by energy until the source ends or listening is paused."""
        import numpy as np  # lazy

        buffer: list[bytes] = []
        silence = 0
        spoke = False
        async for frame in frames:
            if not self.control.listening:
                return  # paused mid-segment: drop the buffered audio, close the mic
            samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(samples**2) + 1e-9))
            if rms >= rms_threshold:
                buffer.append(frame)
                spoke = True
                silence = 0
            elif spoke:
                buffer.append(frame)
                silence += 1
                if silence < silence_frames:
                    continue
                segment = b"".join(buffer)
                buffer, silence, spoke = [], 0, False
                audio = _normalize(segment, np, target_peak) if target_peak > 0 else segment
                await self._handle_segment(audio, len(segment), transcribe, playback)

    async def _handle_segment(
        self,
        audio: bytes,
        raw_len: int,
        transcribe: Callable[[bytes], Awaitable[str]],
        playback: Playback,
    ) -> None:
        text = (await transcribe(audio)).lower().strip()
        # Transcript text is logged locally only; the gateway gets it in test mode.
        _log.info("heard %.1fs: %r", raw_len / 2 / _SAMPLE_RATE, text)
        if self.control.testing():
            self._heard = text
            await self._report()
        if not (text and match_wake(text, self.control.wake_words, self.control.match)):
            return
        _log.info("wake word matched -> sending to the agent")
        self._last_wake_at = time.time()
        await self._set_state("heard")
        wav = await self._client.send_utterance(audio)
        if wav:
            await self._set_state("speaking")
            await playback(wav)
        await self._set_state("idle")


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


async def _alsa_frame_source(device: str, frame_bytes: int = 2560) -> AsyncGenerator[bytes, None]:
    """Yield 16 kHz mono PCM-16 frames from ``arecord`` (no PortAudio needed)."""
    proc = await asyncio.create_subprocess_exec(
        "arecord",
        "-q",
        "-D",
        device,
        "-f",
        "S16_LE",
        "-r",
        str(_SAMPLE_RATE),
        "-c",
        "1",
        "-t",
        "raw",
        stdout=asyncio.subprocess.PIPE,
    )
    if proc.stdout is None:  # pragma: no cover - PIPE always yields a stream
        raise RuntimeError("arecord produced no stdout stream")
    try:
        while True:
            try:
                yield await proc.stdout.readexactly(frame_bytes)
            except asyncio.IncompleteReadError:
                return
    finally:
        proc.terminate()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()


async def _alsa_play(wav: bytes, device: str) -> None:
    """Play WAV bytes through ``aplay`` (no PortAudio needed)."""
    proc = await asyncio.create_subprocess_exec(
        "aplay", "-q", "-D", device, stdin=asyncio.subprocess.PIPE
    )
    await proc.communicate(wav)


def main() -> None:
    """Console entry (`arc-voice`). URI + token come from the environment."""
    uri = os.environ.get("ARC_VOICE_URI", "ws://127.0.0.1:8790")
    token = os.environ.get("ARC_VOICE_TOKEN", "")
    if not token:
        raise SystemExit("set ARC_VOICE_TOKEN (the pairing token) to connect")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s voice-client %(message)s")
    wake_model = os.environ.get("ARC_VOICE_WAKE_MODEL")
    always_on = os.environ.get("ARC_VOICE_ALWAYS_ON")  # STT-wake, no model needed
    desk = VoiceDeskClient(VoiceClient(uri=uri, token=token))
    try:
        if wake_model:
            print(f"Always-on 'hey Olivia' ({wake_model}) → {uri}  (Ctrl-C to quit)")  # noqa: T201
            asyncio.run(desk.run_always_on(detector=OpenWakeWordDetector(model_path=wake_model)))
        elif always_on:
            mic = os.environ.get("ARC_VOICE_MIC", "plughw:CARD=MV7i,DEV=0")
            spk = os.environ.get("ARC_VOICE_SPEAKER", mic)
            from arcgateway.adapters.voice.engine.stt import WhisperSTT

            stt = WhisperSTT(model=os.environ.get("ARC_VOICE_STT", "tiny"))
            pinned = tuple(
                w.strip().lower()
                for w in os.environ.get("ARC_VOICE_WAKE_WORDS", "").split(",")
                if w.strip()
            )
            print(f"Always-on via {mic} → {uri}  (Ctrl-C to quit)")  # noqa: T201

            async def _play(wav: bytes) -> None:
                await _alsa_play(wav, spk)

            asyncio.run(
                desk.run_stt_wake(
                    open_frames=lambda: _alsa_frame_source(mic),
                    transcribe=stt.listen,
                    playback=_play,
                    wake_words=pinned or None,
                    mic=mic,
                    rms_threshold=float(os.environ.get("ARC_VOICE_RMS", "500")),
                    target_peak=float(os.environ.get("ARC_VOICE_TARGET_PEAK", "0")),
                )
            )
        else:
            print(f"Push-to-talk → {uri}  (Ctrl-C to quit)")  # noqa: T201 - CLI user output
            asyncio.run(desk.run_loop(capture=_default_capture, playback=_default_playback))
    except KeyboardInterrupt:
        print("\nbye")  # noqa: T201 - CLI user output


__all__ = ["Capture", "ClientControl", "Playback", "VoiceDeskClient", "main"]

if __name__ == "__main__":
    main()
