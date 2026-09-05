"""WakeGate — "hey Olivia" or push-to-talk (SPEC-077 COMP-006, REQ-007/011).

Gates streaming so **no audio leaves the client before a wake** (REQ-007): frames
are fed to a local detector and nothing streams until it fires (or the operator
uses push-to-talk). The detector is injected — the real one wraps a self-trained
openWakeWord ONNX model (follow-up); the gate logic here is model-free and tested.
"""

from __future__ import annotations

from typing import Protocol


class WakeDetector(Protocol):
    """Local wake-word decision for one audio frame."""

    def detect(self, frame: bytes) -> bool: ...


class WakeGate:
    """Idle until wake; then awake until reset. Streaming is gated on `is_awake`."""

    def __init__(self, detector: WakeDetector | None = None) -> None:
        self._detector = detector
        self._awake = False

    def on_frame(self, frame: bytes) -> bool:
        """Feed one frame. Returns True on the idle→awake transition only.

        While idle this returns False, which is the signal *not* to stream — audio
        never leaves the client before the wake word fires (REQ-007).
        """
        if self._awake:
            return False
        if self._detector is not None and self._detector.detect(frame):
            self._awake = True
            return True
        return False

    def push_to_talk(self) -> bool:
        """Manual wake (Mac has a keyboard). Returns True if it woke from idle."""
        was_awake = self._awake
        self._awake = True
        return not was_awake

    def is_awake(self) -> bool:
        return self._awake

    def reset(self) -> None:
        """Back to idle after an utterance completes — re-arm the wake word."""
        self._awake = False


__all__ = ["WakeDetector", "WakeGate"]
