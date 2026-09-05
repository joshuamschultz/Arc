"""T-034 — the voice journey over a real socket (SPEC-077 REQ-001/002/005).

Drives the whole path a user drives: a paired client sends an utterance over a
real WebSocket, the adapter runs the turn (STT -> agent -> reply -> TTS), and the
spoken reply comes back down the same link. Only the models are faked (the audio
and LLM wires); the transport, adapter, pairing and delivery are all real.
"""

from __future__ import annotations

from arcgateway.adapters.voice.adapter import VoiceAdapter
from arcgateway.adapters.voice.engine.base import FakeVoiceEngine
from arcgateway.adapters.voice.transport import VoiceClient
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import TextPart


async def test_desk_to_agent_and_back_over_a_real_socket() -> None:
    engine = FakeVoiceEngine(transcript="hey olivia what is on my calendar")
    seen: dict[str, str] = {}
    adapter: VoiceAdapter

    async def on_message(draft: object) -> None:
        seen["transcript"] = draft.parts[0].text  # type: ignore[attr-defined]
        await adapter.send(
            DeliveryTarget(platform="voice", chat_id=draft.chat_id),  # type: ignore[attr-defined]
            [TextPart(text="You have two meetings today.")],
        )

    adapter = VoiceAdapter(
        on_message=on_message,
        agent_did="did:arc:olivia",
        engine=engine,
        host="127.0.0.1",
        port=0,
        authenticate=lambda t: ("did:arc:josh", "mic:desk") if t == "pair-tok" else None,
    )
    await adapter.connect()
    try:
        client = VoiceClient(uri=f"ws://127.0.0.1:{adapter.bound_port()}", token="pair-tok")
        await client.connect()
        assert client.chat_id == "mic:desk"
        reply = await client.send_utterance(b"\x00\x01pretend-utterance")
        assert seen["transcript"] == "hey olivia what is on my calendar"
        assert reply is not None
        assert b"two meetings" in reply  # the fake TTS voices the (shortened) reply text
        await client.close()
    finally:
        await adapter.disconnect()
