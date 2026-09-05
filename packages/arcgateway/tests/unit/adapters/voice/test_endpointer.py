"""T-015 (RED) — endpointing (SPEC-077 COMP-008, REQ-010).

"You're done talking" is a tuned trailing-silence timeout over a VAD. The VAD
itself is injected (Silero in production) so the segmentation logic — one
utterance per speech run closed by a hangover of silence — is tested without a
model. Frame content stands in for the VAD decision here.
"""

from __future__ import annotations

from arcgateway.adapters.voice.endpoint import Endpointer


class _FakeVAD:
    """b"S" is speech, anything else is silence."""

    def is_speech(self, frame: bytes) -> bool:
        return frame == b"S"


def _ep() -> Endpointer:
    # 20 ms frames, 700 ms hangover -> 35 silence frames close an utterance.
    return Endpointer(_FakeVAD(), frame_ms=20, hangover_ms=700)


def test_speech_then_trailing_silence_is_one_utterance() -> None:
    utts = _ep().segment([b"S"] * 5 + [b"."] * 40)
    assert len(utts) == 1
    assert len(utts[0].frames) == 5


def test_two_speech_runs_separated_by_a_full_hangover_are_two() -> None:
    frames = [b"S"] * 5 + [b"."] * 35 + [b"S"] * 3 + [b"."] * 35
    assert len(_ep().segment(frames)) == 2


def test_a_gap_shorter_than_the_hangover_stays_one_utterance() -> None:
    frames = [b"S"] * 5 + [b"."] * 10 + [b"S"] * 5 + [b"."] * 40
    utts = _ep().segment(frames)
    assert len(utts) == 1
    assert len(utts[0].frames) == 10


def test_silence_only_yields_no_utterance() -> None:
    assert _ep().segment([b"."] * 50) == []


def test_trailing_speech_at_stream_end_still_closes() -> None:
    assert len(_ep().segment([b"S"] * 4 + [b"."] * 10)) == 1
