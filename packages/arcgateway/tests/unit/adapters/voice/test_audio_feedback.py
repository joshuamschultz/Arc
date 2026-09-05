"""T-026 (RED) — audio feedback + interruption (SPEC-077 COMP-012, REQ-015, D-767).

No screen, so state is conveyed by a small fixed earcon set. Barge-in stops on a
real interruption but ignores backchannels ("mm-hmm"), and the bar is raised
during a confirmation read-back so noise cannot commit or cancel.
"""

from __future__ import annotations

from arcgateway.adapters.voice.ux.audio_feedback import (
    DialogueState,
    Earcon,
    Interruption,
    earcon_for,
)


def test_every_state_maps_to_an_earcon() -> None:
    assert earcon_for(DialogueState.WOKE) is Earcon.WAKE
    assert earcon_for(DialogueState.LISTENING) is Earcon.LISTENING
    assert earcon_for(DialogueState.PROCESSING) is Earcon.WORKING
    assert earcon_for(DialogueState.SUCCEEDED) is Earcon.DONE
    assert earcon_for(DialogueState.FAILED) is Earcon.ERROR


def test_backchannel_does_not_stop_the_assistant() -> None:
    intr = Interruption()
    assert intr.classify("mm-hmm") == "backchannel"
    assert intr.should_stop("mm-hmm") is False


def test_a_real_interruption_stops_the_assistant() -> None:
    intr = Interruption()
    assert intr.classify("no wait stop") == "interrupt"
    assert intr.should_stop("no wait stop") is True


def test_empty_input_is_ignored() -> None:
    assert Interruption().classify("   ") == "ignore"


def test_raised_bar_during_readback_ignores_short_noise() -> None:
    intr = Interruption()
    # A single stray noise token must not be taken as an answer to a read-back.
    assert intr.should_stop("uh", during_readback=True) is False
    # A real command word still gets through.
    assert intr.should_stop("cancel", during_readback=True) is True
