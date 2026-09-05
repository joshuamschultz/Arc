"""T-024 (RED) — the confirmation gate (SPEC-077 COMP-011, REQ-014/018, D-766).

The load-bearing security guard for a voice agent that takes real actions. An
irreversible action is not committed without a fresh explicit "yes" spoken against
a read-back of the ACTUAL parameters; ambiguity or silence aborts. Reversible
actions confirm implicitly. The human is the commit authority — this defends
against both mis-recognition and injected/agent-initiated actions.
"""

from __future__ import annotations

from arcgateway.adapters.voice.ux.confirmation import (
    ActionRisk,
    ConfirmationGate,
    PendingAction,
)


def _send() -> PendingAction:
    return PendingAction(
        description="send a message to Dana",
        params={"to": "Dana", "text": "running late"},
        risk=ActionRisk.IRREVERSIBLE,
    )


def _play() -> PendingAction:
    return PendingAction(description="play music", params={}, risk=ActionRisk.REVERSIBLE)


def test_reversible_action_confirms_implicitly_no_readback() -> None:
    gate = ConfirmationGate()
    assert gate.readback(_play()) is None
    assert gate.is_confirmed(_play(), reply="") is True


def test_irreversible_readback_names_the_actual_params() -> None:
    speech = ConfirmationGate().readback(_send())
    assert speech is not None
    assert "Dana" in speech
    assert "running late" in speech


def test_irreversible_needs_an_explicit_yes() -> None:
    gate = ConfirmationGate()
    assert gate.is_confirmed(_send(), reply="yes") is True
    assert gate.is_confirmed(_send(), reply="go ahead") is True


def test_ambiguous_reply_aborts() -> None:
    gate = ConfirmationGate()
    assert gate.is_confirmed(_send(), reply="maybe later") is False


def test_silence_aborts() -> None:
    gate = ConfirmationGate()
    assert gate.is_confirmed(_send(), reply="") is False


def test_explicit_no_aborts() -> None:
    gate = ConfirmationGate()
    assert gate.is_confirmed(_send(), reply="no cancel that") is False


def test_interpret_grammar_is_bounded() -> None:
    gate = ConfirmationGate()
    assert gate.interpret("yes") == "yes"
    assert gate.interpret("cancel") == "no"
    assert gate.interpret("what time is it") == "ambiguous"
