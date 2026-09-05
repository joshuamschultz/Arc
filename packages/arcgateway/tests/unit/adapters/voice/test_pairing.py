"""T-028 — voice pairing (SPEC-077 COMP-013, REQ-017).

No pairing token -> no agent response. A valid token maps to the operator
identity; a wrong, empty, or (unconfigured) token is refused, fail-closed.
"""

from __future__ import annotations

from arcgateway.adapters.voice.pairing import VoicePairing


def _paired() -> VoicePairing:
    return VoicePairing(token="s3cret-token", operator_did="did:arc:josh", chat_id="voice")


def test_valid_token_authorizes_the_operator() -> None:
    assert _paired().authenticate("s3cret-token") == ("did:arc:josh", "voice")


def test_wrong_token_is_refused() -> None:
    assert _paired().authenticate("not-it") is None


def test_empty_token_is_refused() -> None:
    assert _paired().authenticate("") is None


def test_unconfigured_pairing_refuses_everything() -> None:
    unpaired = VoicePairing(token=None, operator_did="did:arc:josh", chat_id="voice")
    assert unpaired.is_configured() is False
    assert unpaired.authenticate("anything") is None
    assert unpaired.authenticate("") is None
