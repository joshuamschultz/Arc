"""T-013 — the wake gate (SPEC-077 COMP-006, REQ-007/011).

No audio streams before a wake; the wake word and push-to-talk both wake it.
"""

from __future__ import annotations

from arcgateway.adapters.voice.wake import WakeGate


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
