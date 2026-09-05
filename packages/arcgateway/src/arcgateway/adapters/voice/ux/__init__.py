"""Voice UX guard modules (SPEC-077 COMP-009..012).

Four small, reusable, event/state-driven pieces that keep a voice turn safe and
legible: the output contract (ear-friendly, injection-safe replies), the progress
manager (never-silent pacing), the confirmation gate (the load-bearing security
guard), and audio feedback + interruption (earcons + barge-in). Deepen R-4.
"""

from arcgateway.adapters.voice.ux.audio_feedback import (
    DialogueState,
    Earcon,
    Interruption,
    earcon_for,
)
from arcgateway.adapters.voice.ux.confirmation import (
    ActionRisk,
    ConfirmationGate,
    PendingAction,
)
from arcgateway.adapters.voice.ux.output_contract import OutputContract, SpokenReply
from arcgateway.adapters.voice.ux.progress import ProgressEvent, ProgressManager

__all__ = [
    "ActionRisk",
    "ConfirmationGate",
    "DialogueState",
    "Earcon",
    "Interruption",
    "OutputContract",
    "PendingAction",
    "ProgressEvent",
    "ProgressManager",
    "SpokenReply",
    "earcon_for",
]
