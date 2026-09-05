"""Endpointer — "you're done talking" (SPEC-077 COMP-008, REQ-010, D-763).

Segments an audio frame stream into utterances: a run of speech is closed once a
hangover of trailing silence passes. The VAD is injected (Silero over
echo-cancelled audio in production; WebRTC-VAD is the cheap gate upstream), so
this segmentation logic is model-free and testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


class VAD(Protocol):
    """Voice-activity decision for one frame."""

    def is_speech(self, frame: bytes) -> bool: ...


@dataclass
class Utterance:
    """The speech frames of one detected utterance (trailing silence dropped)."""

    frames: list[bytes] = field(default_factory=list)


class Endpointer:
    """Turn a frame stream into utterances via a tuned trailing-silence hangover."""

    def __init__(self, vad: VAD, *, frame_ms: int = 20, hangover_ms: int = 700) -> None:
        self._vad = vad
        self._hangover_frames = max(1, round(hangover_ms / frame_ms))

    def segment(self, frames: Iterable[bytes]) -> list[Utterance]:
        utterances: list[Utterance] = []
        current: list[bytes] = []
        silence = 0
        in_utterance = False

        for frame in frames:
            if self._vad.is_speech(frame):
                in_utterance = True
                current.append(frame)
                silence = 0
            elif in_utterance:
                silence += 1
                if silence >= self._hangover_frames:
                    utterances.append(Utterance(frames=current))
                    current = []
                    silence = 0
                    in_utterance = False

        if in_utterance and current:
            utterances.append(Utterance(frames=current))
        return utterances


__all__ = ["VAD", "Endpointer", "Utterance"]
