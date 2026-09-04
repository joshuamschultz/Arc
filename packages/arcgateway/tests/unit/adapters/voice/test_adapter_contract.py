"""T-003 (RED) — VoiceAdapter satisfies BasePlatformAdapter (SPEC-077 COMP-001).

Lifecycle + to_parts + send, and nothing more (the gateway keeps audit, session,
splitting). Inbound is a finished utterance {"transcript": ...} turned into
TextParts; outbound parts are voiced by the engine; a reply stays on the voice
origin target (REQ-002).
"""

from __future__ import annotations

from arcgateway.adapters.base import BasePlatformAdapter
from arcgateway.adapters.registry import AdapterBuildContext, AdapterSpec
from arcgateway.adapters.voice import PLATFORM, build
from arcgateway.adapters.voice.engine.base import FakeVoiceEngine
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import TextPart


async def _noop(_event: object) -> None:  # pragma: no cover
    return None


def _ctx() -> AdapterBuildContext:
    return AdapterBuildContext(
        name="voice",
        raw_config={"enabled": True},
        on_message=_noop,
        default_agent_did="did:arc:agent",
        tier="personal",
    )


def test_platform_descriptor_is_a_named_adapter_spec() -> None:
    assert isinstance(PLATFORM, AdapterSpec)
    assert PLATFORM.name == "voice"
    assert not isinstance(PLATFORM.supports, str)
    assert all(isinstance(s, str) for s in PLATFORM.supports)


def test_adapter_satisfies_the_base_protocol() -> None:
    adapter = build(_ctx())
    assert isinstance(adapter, BasePlatformAdapter)
    assert adapter.name == "voice"
    assert adapter.agent_did == "did:arc:agent"


def test_to_parts_turns_an_utterance_into_text_parts() -> None:
    adapter = build(_ctx())
    parts = adapter.to_parts({"transcript": "what's on my calendar"})
    assert parts == [TextPart(text="what's on my calendar")]


def test_to_parts_is_empty_for_a_blank_utterance() -> None:
    adapter = build(_ctx())
    assert adapter.to_parts({"transcript": "   "}) == []
    assert adapter.to_parts({}) == []


async def test_send_voices_the_reply_through_the_engine() -> None:
    engine = FakeVoiceEngine()
    adapter = build(_ctx())
    adapter.engine = engine  # inject the fake for observation
    target = DeliveryTarget(platform="voice", chat_id="mic:desk")
    await adapter.send(target, [TextPart(text="You have two meetings.")])
    assert engine.spoken == ["You have two meetings."]
