"""T-013 — the wake gate (SPEC-077 COMP-006, REQ-007/011).

No audio streams before a wake; the wake word and push-to-talk both wake it.
"""

from __future__ import annotations

import queue

import numpy as np

from arcgateway.adapters.voice.client import _collect_utterance
from arcgateway.adapters.voice.wake import OpenWakeWordDetector, WakeGate


class _FakeOWW:
    def __init__(self, score: float) -> None:
        self.score = score

    def predict(self, _samples: object) -> dict[str, float]:
        return {"hey_olivia": self.score}


def test_oww_detector_fires_only_above_threshold() -> None:
    frame = np.zeros(1280, dtype=np.int16).tobytes()
    assert OpenWakeWordDetector(model_path="x", threshold=0.5, model=_FakeOWW(0.9)).detect(frame)
    assert not OpenWakeWordDetector(model_path="x", threshold=0.5, model=_FakeOWW(0.2)).detect(frame)


def test_collect_utterance_stops_after_trailing_silence() -> None:
    q: queue.Queue[bytes] = queue.Queue()
    loud = (np.ones(1280, dtype=np.int16) * 3000).tobytes()
    quiet = np.zeros(1280, dtype=np.int16).tobytes()
    for _ in range(3):
        q.put(loud)
    for _ in range(20):
        q.put(quiet)
    pcm = _collect_utterance(q, np, silence_frames=15)
    assert len(pcm) == (3 + 15) * 1280 * 2  # 3 speech + 15 trailing-silence frames


def test_collect_utterance_returns_empty_on_pure_silence() -> None:
    q: queue.Queue[bytes] = queue.Queue()
    quiet = np.zeros(1280, dtype=np.int16).tobytes()
    for _ in range(20):
        q.put(quiet)
    assert _collect_utterance(q, np, silence_frames=15) == b""


class _FakeDetector:
    """Fires on the sentinel frame b"WAKE"."""

    def detect(self, frame: bytes) -> bool:
        return frame == b"WAKE"


def test_no_detector_never_streams_before_a_wake() -> None:
    gate = WakeGate()
    assert gate.on_frame(b"ambient noise") is False
    assert gate.is_awake() is False  # nothing streams while idle (REQ-007)


def test_wake_word_wakes_once_then_stays_awake() -> None:
    gate = WakeGate(_FakeDetector())
    assert gate.on_frame(b"...") is False
    assert gate.on_frame(b"WAKE") is True  # idle -> awake transition
    assert gate.is_awake() is True
    assert gate.on_frame(b"WAKE") is False  # already awake, no re-trigger


def test_push_to_talk_wakes_from_idle() -> None:
    gate = WakeGate(_FakeDetector())
    assert gate.push_to_talk() is True
    assert gate.is_awake() is True
    assert gate.push_to_talk() is False  # already awake


def test_reset_rearms_the_wake_word() -> None:
    gate = WakeGate(_FakeDetector())
    gate.on_frame(b"WAKE")
    gate.reset()
    assert gate.is_awake() is False
    assert gate.on_frame(b"WAKE") is True  # re-armed
