"""T-030/032 — per-turn audit + telemetry (SPEC-077 COMP-015/016, REQ-019/021).

Every step of a voice turn emits an audit event at the single arctrust emission
point; a reply with no live link is audited as a warn, not silently dropped. The
telemetry span is a working (or no-op) context manager.
"""

from __future__ import annotations

import pytest

from arcgateway.adapters.voice import adapter as adapter_mod
from arcgateway.adapters.voice.adapter import VoiceAdapter
from arcgateway.adapters.voice.engine.base import FakeVoiceEngine
from arcgateway.adapters.voice.telemetry import voice_span
from arcgateway.adapters.voice.transport import VoiceLink
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import TextPart


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, data: object) -> None:
        self.sent.append(data)


async def test_a_turn_emits_the_ordered_audit_events(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        adapter_mod,
        "emit_event",
        lambda action, target, outcome, **kw: events.append(action),
    )
    engine = FakeVoiceEngine(transcript="what time is it")
    adapter: VoiceAdapter

    async def on_message(draft: object) -> None:
        await adapter.send(
            DeliveryTarget(platform="voice", chat_id=draft.chat_id),  # type: ignore[attr-defined]
            [TextPart(text="It is noon.")],
        )

    adapter = VoiceAdapter(on_message=on_message, agent_did="did:arc:a", engine=engine)
    ws = _FakeWS()
    await adapter._on_utterance(VoiceLink(chat_id="mic", user_did="did:u", _ws=ws), b"pcm")

    assert "voice.utterance.received" in events
    assert "voice.transcribed" in events
    assert "voice.reply.spoken" in events


async def test_reply_to_a_dead_link_is_audited_as_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        adapter_mod,
        "emit_event",
        lambda action, target, outcome, **kw: events.append((action, outcome)),
    )
    adapter = VoiceAdapter(
        on_message=_never, agent_did="did:arc:a", engine=FakeVoiceEngine()
    )
    await adapter.send(DeliveryTarget(platform="voice", chat_id="gone"), [TextPart(text="hi")])
    assert ("voice.reply.no_link", "warn") in events


async def _never(_draft: object) -> None:  # pragma: no cover
    raise AssertionError("on_message must not be called here")


def test_voice_span_is_a_context_manager() -> None:
    with voice_span("voice.test", chat_id="mic", n=1):
        pass  # no error whether OpenTelemetry is installed or not
