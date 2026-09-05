"""AudioFeedback + Interruption (SPEC-077 COMP-012, REQ-015, D-767).

With no screen, dialogue state is conveyed by a small fixed earcon set. Barge-in
stops on a real interruption but ignores backchannels ("mm-hmm"), because a false
cut-off erodes trust faster than a missed one. During a confirmation read-back the
bar is raised so a stray noise token cannot be taken as an answer.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

Classification = Literal["backchannel", "interrupt", "ignore"]


class Earcon(Enum):
    WAKE = "wake"
    LISTENING = "listening"
    WORKING = "working"
    DONE = "done"
    ERROR = "error"


class DialogueState(Enum):
    WOKE = "woke"
    LISTENING = "listening"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_STATE_EARCON: dict[DialogueState, Earcon] = {
    DialogueState.WOKE: Earcon.WAKE,
    DialogueState.LISTENING: Earcon.LISTENING,
    DialogueState.PROCESSING: Earcon.WORKING,
    DialogueState.SUCCEEDED: Earcon.DONE,
    DialogueState.FAILED: Earcon.ERROR,
}

#: Acknowledgement tokens that mean "keep going", not "stop".
_BACKCHANNELS = frozenset(
    {"mm-hmm", "mmhmm", "mhm", "uh-huh", "uhhuh", "yeah", "right", "okay", "ok", "sure", "go on"}
)
#: Words that always count as real input, even under a raised bar.
_COMMANDS = frozenset({"yes", "no", "cancel", "stop", "confirm", "nope", "wait", "abort"})


def earcon_for(state: DialogueState) -> Earcon:
    return _STATE_EARCON[state]


class Interruption:
    """Classify speech heard while the assistant is talking."""

    def classify(self, utterance: str, *, during_readback: bool = False) -> Classification:
        norm = " ".join(utterance.strip().lower().split())
        if not norm:
            return "ignore"
        if norm in _BACKCHANNELS:
            return "backchannel"
        words = norm.split()
        if during_readback and len(words) < 2 and not (set(words) & _COMMANDS):
            # Raised bar: a single non-command token is noise, not an answer.
            return "ignore"
        return "interrupt"

    def should_stop(self, utterance: str, *, during_readback: bool = False) -> bool:
        return self.classify(utterance, during_readback=during_readback) == "interrupt"


__all__ = [
    "Classification",
    "DialogueState",
    "Earcon",
    "Interruption",
    "earcon_for",
]
