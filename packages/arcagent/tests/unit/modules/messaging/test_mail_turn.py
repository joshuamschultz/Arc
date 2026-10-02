"""The mail turn: an inbound mail wakes the agent and its answer lands in the thread.

Before this, a mail turn ran with no reply target, so the run's final text was
dropped; the wake prompt showed a raw DID with no subject or thread; and a
manual ``messaging_send`` reply opened a new thread. Mail is one message plus at
most one reply: conversation belongs in the team channel.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcteam.mail import MailSendResult, MailThreadClosedError
from arcteam.types import DeliveryKind, Entity, EntityType, Message
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.core import known_channels, turn_context
from arcagent.core.module_bus import EventContext
from arcagent.modules.messaging import _runtime, activation, mail_turn
from arcagent.modules.messaging.capabilities import (
    _handle_incoming,
    _notify_target,
    _origin_reply_target,
    deliver_origin_reply,
    messaging_send,
)

_CONVERSATION = "conversation_q3"


class _FakeMail:
    """Records what the mail turn asks of ``AgentMailService``."""

    def __init__(self, *, participants: tuple[str, ...] = ()) -> None:
        self.replies: list[tuple[str, str, str]] = []
        self.sent: list[Any] = []
        self.closed = False
        self.refuse: Exception | None = None
        self.participants = participants

    async def reply_to_conversation(
        self,
        conversation_id: str,
        *,
        body: str,
        idempotency_key: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Any:
        if self.closed:
            raise MailThreadClosedError()
        self.replies.append((conversation_id, body, idempotency_key))
        return SimpleNamespace(message_id="m1", thread_id="t1")

    async def has_reply(self, conversation_id: str, *, idempotency_key: str, **_: Any) -> bool:
        return any(r[0] == conversation_id and r[2] == idempotency_key for r in self.replies)

    async def send(self, request: Any) -> MailSendResult:
        self.sent.append(request)
        return MailSendResult(message_id="m2", thread_id="conversation_new", status="sent")

    async def check_inbound(self, message: Any, **_: Any) -> None:
        if self.refuse is not None:
            raise self.refuse

    async def conversation_participants(self, conversation_id: str, **_: Any) -> tuple[str, ...]:
        return self.participants


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    turn_context.set_inbound_channel(None)
    yield
    _runtime.reset()
    turn_context.set_inbound_channel(None)


@pytest.fixture
def alice() -> AgentIdentity:
    return AgentIdentity.generate(org="local", agent_type="agent")


def _configure(tmp_path: Path, alice: AgentIdentity, mail: _FakeMail) -> Any:
    me = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config=make_config_dict(entity_id="agent://me", entity_name="me"),
        workspace=tmp_path,
        identity=me,
        operator_signer=make_operator_signer(),
    )
    st = _runtime.state()
    st.mail_service = mail
    st.arcstore_opener = object()
    st.telemetry = MagicMock()
    alice_entity = Entity(
        did=alice.did,
        handle="alice",
        id="agent://alice",
        name="Alice",
        type=EntityType.AGENT,
        public_key=alice.public_key.hex(),
    )
    st.registry = MagicMock()
    st.registry.get = AsyncMock(side_effect=lambda did: alice_entity if did == alice.did else None)
    st.registry.list_entities = AsyncMock(return_value=[alice_entity])
    return st


def _mail(alice: AgentIdentity, **overrides: Any) -> Message:
    data: dict[str, Any] = {
        "id": "message_ask",
        "ts": datetime.now(UTC).isoformat(),
        "sender": alice.did,
        "signer_did": alice.did,
        "to": ["agent://me"],
        "delivery_kind": DeliveryKind.MAIL,
        "subject": "Q3 numbers",
        "body": "What was Q3 revenue? @bob may know too.",
        "thread_id": _CONVERSATION,
        "mentions": ["did:arc:local:agent/bob"],
    }
    data.update(overrides)
    return Message(**data)


def _respond(final_text: str, *, run_id: str | None = "run-1") -> EventContext:
    data: dict[str, Any] = {
        "result": None,
        "messages": [
            {"role": "user", "content": "mail"},
            {"role": "assistant", "content": final_text},
        ],
        "session_id": "s1",
        "automated": True,
    }
    if run_id is not None:
        data["run_id"] = run_id
    return EventContext(
        event="agent:post_respond", data=data, agent_did="did:arc:local:agent/me", trace_id="t"
    )


def test_mail_reply_target_names_the_conversation(alice: AgentIdentity) -> None:
    assert _origin_reply_target(_mail(alice)) == (
        f"mail://{_CONVERSATION}",
        "Mail — Q3 numbers",
    )


async def test_wake_prompt_shows_handle_subject_thread_and_the_reply_policy(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    st = _configure(tmp_path, alice, _FakeMail())

    prompt = await mail_turn.format_delivery(st, _mail(alice))

    assert "@alice" in prompt
    assert alice.did not in prompt
    assert "Q3 numbers" in prompt
    assert _CONVERSATION in prompt
    assert "What was Q3 revenue?" in prompt
    assert "posted to this thread automatically" in prompt
    assert "finish without" in prompt
    assert "team channel" in prompt
    assert "create_task" in prompt
    assert "mail_handoff" in prompt


async def test_a_copied_recipient_is_told_no_reply_is_expected(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    st = _configure(tmp_path, alice, _FakeMail())

    prompt = await mail_turn.format_delivery(
        st, _mail(alice, to=["agent://carol"], cc=["agent://me"])
    )

    assert "copied" in prompt


async def test_finalizer_posts_the_final_text_into_the_thread_once(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    mail = _FakeMail()
    st = _configure(tmp_path, alice, mail)
    turn_context.set_inbound_channel(f"mail://{_CONVERSATION}")

    await deliver_origin_reply(_respond("Q3 revenue was 4.2M."))

    assert mail.replies == [(_CONVERSATION, "Q3 revenue was 4.2M.", "run:run-1")]
    events = [call.args[0] for call in st.telemetry.audit_event.call_args_list]
    assert "messaging.mail_reply" in events


async def test_fyi_mail_with_no_final_text_finishes_silently(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    mail = _FakeMail()
    _configure(tmp_path, alice, mail)
    turn_context.set_inbound_channel(f"mail://{_CONVERSATION}")

    await deliver_origin_reply(_respond("   "))

    assert mail.replies == []


async def test_a_closed_thread_is_skipped_without_crashing_the_finalizer(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    mail = _FakeMail()
    mail.closed = True
    st = _configure(tmp_path, alice, mail)
    turn_context.set_inbound_channel(f"mail://{_CONVERSATION}")

    await deliver_origin_reply(_respond("late answer"))

    assert mail.replies == []
    outcomes = [
        call.args[1].get("outcome")
        for call in st.telemetry.audit_event.call_args_list
        if call.args[0] == "messaging.mail_reply"
    ]
    assert outcomes == ["thread_closed"]


async def test_messaging_send_to_a_did_takes_the_mail_path(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    mail = _FakeMail()
    _configure(tmp_path, alice, mail)

    result = json.loads(await messaging_send(to=alice.did, body="Ping about Q4"))

    assert result["status"] == "sent"
    assert mail.sent[0].to == (alice.did,)


async def test_messaging_send_back_to_the_sender_in_a_mail_turn_is_the_one_reply(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    from arctrust.causal import run_scope

    st = _configure(tmp_path, alice, _FakeMail())
    mail = _FakeMail(participants=(alice.did, st.identity.did))
    st.mail_service = mail
    turn_context.set_inbound_channel(f"mail://{_CONVERSATION}")

    with run_scope("run-7"):
        result = json.loads(await messaging_send(to="agent://alice", body="4.2M"))

    assert result["thread_id"] == _CONVERSATION
    assert mail.replies == [(_CONVERSATION, "4.2M", "run:run-7")]
    assert mail.sent == []


async def test_mail_turn_is_never_remembered_as_a_known_channel(tmp_path: Path) -> None:
    from arcagent.core.agent_dispatch import bind_inbound_channel

    agent = SimpleNamespace(_workspace=tmp_path)

    bind_inbound_channel(agent, f"mail://{_CONVERSATION}", "Mail — Q3")  # type: ignore[arg-type]  # reason: dispatch reads only _workspace

    assert known_channels.list_channels(tmp_path) == []
    assert turn_context.inbound_channel() == f"mail://{_CONVERSATION}"


async def test_notify_user_in_a_mail_turn_does_not_target_the_mail_thread(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    st = _configure(tmp_path, alice, _FakeMail())
    turn_context.set_inbound_channel(f"mail://{_CONVERSATION}")

    assert _notify_target(st) is None


async def test_mail_with_a_mention_of_another_agent_still_wakes_its_recipient(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    st = _configure(tmp_path, alice, _FakeMail())

    decision = await activation.decide(_mail(alice), st)

    assert decision.wake is True
    assert decision.reason == "mail"


async def test_admitted_mail_wakes_with_the_mail_target_and_prompt(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    st = _configure(tmp_path, alice, _FakeMail())
    st.deliver_fn = AsyncMock()

    await _handle_incoming(_mail(alice))

    st.deliver_fn.assert_awaited_once()
    kwargs = st.deliver_fn.await_args.kwargs
    assert kwargs["reply_target"] == f"mail://{_CONVERSATION}"
    assert "@alice" in kwargs["message"]


@pytest.mark.parametrize(
    ("refusal", "reason"),
    [(MailThreadClosedError(), "mail_thread_closed"), (KeyError("x"), "mail_not_durable")],
)
async def test_refused_mail_never_wakes_the_agent(
    tmp_path: Path, alice: AgentIdentity, refusal: Exception, reason: str
) -> None:
    mail = _FakeMail()
    mail.refuse = refusal
    st = _configure(tmp_path, alice, mail)
    st.deliver_fn = AsyncMock()

    await _handle_incoming(_mail(alice))

    st.deliver_fn.assert_not_awaited()
    reasons = [
        call.args[1].get("reason")
        for call in st.telemetry.audit_event.call_args_list
        if call.args[0] == "messaging.activation"
    ]
    assert reasons == [reason]


async def test_mail_handoff_forwards_the_mail_and_copies_the_sender(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    from arcagent.modules.messaging.capabilities import mail_handoff

    mail = _FakeMail(participants=(alice.did,))
    _configure(tmp_path, alice, mail)
    turn_context.set_inbound_channel(f"mail://{_CONVERSATION}")
    mail_turn.bind_inbound(
        mail_turn.InboundMail(
            conversation_id=_CONVERSATION,
            sender=alice.did,
            subject="Q3 numbers",
            body="What was Q3 revenue?",
        )
    )

    result = json.loads(await mail_handoff(to="agent://carol", note="Carol owns finance."))

    assert result["status"] == "sent"
    request = mail.sent[0]
    assert request.to == ("agent://carol",)
    assert request.cc == (alice.did,)
    assert request.subject == "Handoff: Q3 numbers"
    assert "Carol owns finance." in request.body
    assert "What was Q3 revenue?" in request.body


async def test_mail_handoff_outside_a_mail_turn_is_refused(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    from arcagent.modules.messaging.capabilities import mail_handoff

    _configure(tmp_path, alice, _FakeMail())

    result = json.loads(await mail_handoff(to="agent://carol", note="x"))

    assert "error" in result


async def test_signed_run_posts_its_mail_reply_through_the_mail_service(
    tmp_path: Path, alice: AgentIdentity
) -> None:
    from arcagent.core.run_contract import ChannelReply
    from arcagent.modules.messaging.signed_delivery import deliver

    mail = _FakeMail()
    st = _configure(tmp_path, alice, mail)
    message = _mail(alice, sig="sig", nonce="n")
    st.trigger_issuer = AsyncMock(return_value=(b"auth", datetime.now(UTC)))
    st.prepare_collected_request = MagicMock(return_value=SimpleNamespace())
    st.agent_run_fn = AsyncMock(return_value=SimpleNamespace(outcome_unknown=None))
    looked_up: list[bool] = []

    async def reply_fn(run_id: str, *, send: Any, lookup: Any) -> str:
        reply = ChannelReply(
            message_id="reply-1", target=f"mail://{_CONVERSATION}", text="4.2M", digest="d"
        )
        await send(reply)
        looked_up.append(await lookup(reply))
        return "sent"

    st.accepted_reply_fn = reply_fn

    outcome = await deliver(
        st,
        message,
        prompt="p",
        session_key="s",
        reply_target=f"mail://{_CONVERSATION}",
        reply_label="Mail — Q3 numbers",
    )

    assert outcome == "sent"
    assert len(mail.replies) == 1
    assert mail.replies[0][0] == _CONVERSATION
    assert mail.replies[0][2].startswith("run:")
    assert looked_up == [True]
