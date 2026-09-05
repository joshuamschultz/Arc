"""Full voice turn (SPEC-077 COMP-001, REQ-001/002/005/012).

Utterance in -> STT -> the agent (via on_message) -> reply -> output contract ->
TTS -> back down the same link. Fakes the audio + LLM wires only, per
feedback_test_what_users_do; proves the pieces are wired together, not just green
in isolation.
"""

from __future__ import annotations

from arcgateway.adapters.voice.adapter import VoiceAdapter
from arcgateway.adapters.voice.engine.base import FakeVoiceEngine
from arcgateway.adapters.voice.transport import VoiceLink
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import TextPart


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, data: object) -> None:
        self.sent.append(data)


async def test_utterance_flows_to_the_agent_and_a_short_reply_comes_back() -> None:
    engine = FakeVoiceEngine(transcript="what is on my calendar today")
    captured: dict[str, object] = {}
    adapter: VoiceAdapter

    async def on_message(draft: object) -> None:
        captured["draft"] = draft
        # simulate the gateway routing to the agent and the agent replying on-screen
        reply = "You have **two** meetings today.\n\n- Standup at 9\n- Design review at 2"
        await adapter.send(
            DeliveryTarget(platform="voice", chat_id=draft.chat_id),  # type: ignore[attr-defined]
            [TextPart(text=reply)],
        )

    adapter = VoiceAdapter(on_message=on_message, agent_did="did:arc:olivia", engine=engine)
    ws = _FakeWS()
    link = VoiceLink(chat_id="mic:desk", user_did="did:arc:josh", _ws=ws)

    await adapter._on_utterance(link, b"\x00\x01\x02audio-pcm")

    # inbound: the transcript reached the agent as a text part on the voice origin
    draft = captured["draft"]
    assert draft.platform == "voice"  # type: ignore[attr-defined]
    assert draft.chat_id == "mic:desk"  # type: ignore[attr-defined]
    assert draft.parts[0].text == "what is on my calendar today"  # type: ignore[attr-defined]

    # outbound: the reply was shortened for the ear (markdown stripped) and voiced
    assert engine.spoken, "no reply was voiced"
    spoken = engine.spoken[0]
    assert "**" not in spoken and "#" not in spoken
    assert "two meetings" in spoken.lower()
    assert ws.sent, "reply audio was not delivered to the origin link"
