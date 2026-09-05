"""T-020 (RED) — the voice output contract (SPEC-077 COMP-009, REQ-012, D-764).

Enforced in CODE, not just a prompt: a prompt asking for brevity is unenforceable
and injection-bypassable. The transform strips markup (also a guard on reading
injected markup aloud), caps length, and collapses long lists — turning an
agent's on-screen reply into something built for the ear.
"""

from __future__ import annotations

from arcgateway.adapters.voice.ux.output_contract import OutputContract, SpokenReply


def test_returns_a_spoken_reply() -> None:
    out = OutputContract().to_speech("Done.")
    assert isinstance(out, SpokenReply)
    assert out.speech == "Done."


def test_strips_markdown_markup() -> None:
    text = "## Summary\nHere is **bold**, _italic_, `code`, and a [link](https://x.com)."
    speech = OutputContract().to_speech(text).speech
    for marker in ("#", "**", "_italic_", "`", "](http"):
        assert marker not in speech
    assert "link" in speech  # link text survives, URL does not
    assert "https://x.com" not in speech


def test_collapses_a_long_list_to_a_few_plus_offer() -> None:
    text = "\n".join(f"- item {i}" for i in range(1, 7))
    speech = OutputContract(max_list_items=3).to_speech(text).speech
    assert "item 1" in speech and "item 2" in speech and "item 3" in speech
    assert "item 5" not in speech
    assert "3 more" in speech


def test_caps_length_and_puts_the_rest_in_detail() -> None:
    text = " ".join(f"word{i}" for i in range(1, 121))  # 120 words
    out = OutputContract(max_words=40).to_speech(text)
    assert len(out.speech.split()) <= 40
    assert out.detail is not None
    assert "word120" in out.detail


def test_short_reply_needs_no_detail() -> None:
    out = OutputContract(max_words=40).to_speech("All set.")
    assert out.detail is None
