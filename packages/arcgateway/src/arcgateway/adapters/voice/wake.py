"""WakeGate — "hey Olivia" or push-to-talk (SPEC-077 COMP-006, REQ-007/011).

Gates streaming so **no audio leaves the client before a wake** (REQ-007): frames
are fed to a local detector and nothing streams until it fires (or the operator
uses push-to-talk). The detector is injected — the real one wraps a self-trained
openWakeWord ONNX model (follow-up); the gate logic here is model-free and tested.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Protocol

MatchMode = Literal["exact", "fuzzy"]

_NON_WORD = re.compile(r"[^a-z' ]+")
_GREETINGS = frozenset({"hey", "ok", "okay", "hi"})
#: Fuzzy matching looks only at the start of the utterance: a name said mid-sentence
#: to someone else is not a wake.
_FUZZY_WINDOW = 3
#: Tokens shorter than this must match exactly — one edit in "max" is a different word.
_FUZZY_MIN_LEN = 5


def _tokens(text: str) -> list[str]:
    return _NON_WORD.sub(" ", text.lower()).split()


def _strip_greeting(tokens: list[str]) -> list[str]:
    return tokens[1:] if tokens and tokens[0] in _GREETINGS else tokens


def _edit_distance_at_most_one(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ by at most one insert, delete or substitute."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if len(a) == len(b):
        return a[i + 1 :] == b[i + 1 :]
    return a[i:] == b[i + 1 :]


def _token_matches(heard: str, wanted: str, *, fuzzy: bool) -> bool:
    if heard == wanted:
        return True
    return fuzzy and len(wanted) >= _FUZZY_MIN_LEN and _edit_distance_at_most_one(heard, wanted)


def _phrase_at(heard: list[str], wanted: list[str], start: int, *, fuzzy: bool) -> bool:
    window = heard[start : start + len(wanted)]
    return len(window) == len(wanted) and all(
        _token_matches(h, w, fuzzy=fuzzy) for h, w in zip(window, wanted, strict=True)
    )


def match_wake(transcript: str, words: tuple[str, ...] | list[str], mode: MatchMode) -> bool:
    """Whole-word wake match on a transcript (pure; no model, no audio).

    A configured word matches when its tokens appear anywhere in the transcript as
    whole words (so "olive" never wakes "olivia"). In ``fuzzy`` mode a word of five
    or more letters also matches with one edit ("alivia"), but only within the first
    three tokens, and a leading ``hey|ok|okay|hi`` is ignored on both sides.
    """
    heard = _strip_greeting(_tokens(transcript))
    for word in words:
        wanted = _strip_greeting(_tokens(word))
        if not wanted:
            continue
        if any(_phrase_at(heard, wanted, i, fuzzy=False) for i in range(len(heard))):
            return True
        if mode == "fuzzy" and any(
            _phrase_at(heard, wanted, i, fuzzy=True) for i in range(_FUZZY_WINDOW)
        ):
            return True
    return False


class WakeDetector(Protocol):
    """Local wake-word decision for one audio frame."""

    def detect(self, frame: bytes) -> bool: ...


class OpenWakeWordDetector:
    """Real "hey Olivia" detector backed by an openWakeWord ONNX model.

    Feed 16 kHz mono PCM-16 frames (openWakeWord uses 80 ms / 1280-sample chunks).
    ``detect`` returns True once any model score crosses the threshold. The model
    path is a self-trained "hey Olivia" ONNX (see the deploy guide's training
    recipe); the heavy import is lazy so this module stays cheap without [voice].
    """

    def __init__(
        self, *, model_path: str, threshold: float = 0.5, model: Any | None = None
    ) -> None:
        self._model_path = model_path
        self._threshold = threshold
        self._model = model  # injectable for tests

    def _ensure(self) -> Any:
        if self._model is None:
            from openwakeword.model import Model  # lazy: optional [voice] dep

            self._model = Model(wakeword_model_paths=[self._model_path])
        return self._model

    def detect(self, frame: bytes) -> bool:
        import numpy as np  # lazy

        samples = np.frombuffer(frame, dtype=np.int16)
        scores = self._ensure().predict(samples)
        return any(score >= self._threshold for score in scores.values())


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


__all__ = ["MatchMode", "OpenWakeWordDetector", "WakeDetector", "WakeGate", "match_wake"]
