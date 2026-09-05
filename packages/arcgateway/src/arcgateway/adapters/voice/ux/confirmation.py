"""ConfirmationGate — the load-bearing security guard (SPEC-077 COMP-011, REQ-014/018).

A voice agent that takes real actions must never commit an irreversible one on a
mis-heard or injected phrase. So an irreversible action is read back with its
ACTUAL parameters and requires a fresh, explicit, bounded "yes"; ambiguity,
silence, or any "no" aborts. Reversible actions confirm implicitly. The human is
the commit authority (LLM01/ASI02/ASI09, lethal trifecta).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

Verdict = Literal["yes", "no", "ambiguous"]


class ActionRisk(Enum):
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class PendingAction:
    """An action awaiting confirmation, with the parameters to read back."""

    description: str
    params: dict[str, Any]
    risk: ActionRisk


#: Bounded affirmation/negation grammar — small on purpose, so noise or a stray
#: word cannot be mistaken for a commit.
_YES_WORDS = frozenset({"yes", "yep", "yeah", "confirm", "confirmed", "correct", "affirmative"})
_YES_PHRASES = ("go ahead", "do it", "send it")
_NO_WORDS = frozenset({"no", "nope", "cancel", "stop", "dont", "abort", "negative"})
_NON_ALPHA = re.compile(r"[^a-z\s]")


class ConfirmationGate:
    """Read back irreversible actions and gate them on an explicit yes."""

    def readback(self, action: PendingAction) -> str | None:
        """The spoken confirmation prompt, or None for a reversible action."""
        if action.risk is ActionRisk.REVERSIBLE:
            return None
        detail = ", ".join(f"{key}: {value}" for key, value in action.params.items())
        prompt = f"About to {action.description}"
        if detail:
            prompt += f" — {detail}"
        return prompt + ". Say yes to confirm, or cancel to stop."

    def interpret(self, reply: str) -> Verdict:
        norm = _NON_ALPHA.sub(" ", reply.strip().lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        if not norm:
            return "ambiguous"
        words = set(norm.split())
        yes_hit = bool(words & _YES_WORDS) or any(phrase in norm for phrase in _YES_PHRASES)
        no_hit = bool(words & _NO_WORDS)
        if yes_hit and no_hit:
            return "ambiguous"  # conflicting signal → fail closed
        if yes_hit:
            return "yes"
        if no_hit:
            return "no"
        return "ambiguous"

    def is_confirmed(self, action: PendingAction, *, reply: str) -> bool:
        """Reversible → always. Irreversible → only a clean, explicit yes."""
        if action.risk is ActionRisk.REVERSIBLE:
            return True
        return self.interpret(reply) == "yes"


__all__ = ["ActionRisk", "ConfirmationGate", "PendingAction", "Verdict"]
