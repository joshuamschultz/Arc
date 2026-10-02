"""Mail is one message plus at most one reply (alpha-2 item 3, user decision).

Conversation belongs in the team channel, where the operator and every agent can
see it. Mail carries a task, a question or an info share, and gets at most one
answer. A reply to a reply, or a second reply, is refused with a typed error
that tells the agent where to continue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.inbox import Participant, ParticipantRole
from arcstore.inbox_projection import DurableInboxService, thread_id_for
from arcstore.mail_outbox import MailOutbox
from arctrust import AgentIdentity
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository

from arcteam.crypto import MessageSigner
from arcteam.mail import (
    AgentMailService,
    MailSendRequest,
    MailThreadClosedError,
    conversation_of,
    mail_target,
)
from arcteam.types import Message


class _Book:
    def __init__(self, entries: dict[str, str]) -> None:
        self._entries = entries

    async def did_for(self, address: str) -> str:
        if address.startswith("did:"):
            return address
        return self._entries[address]

    async def address_for(self, did: str) -> str:
        for address, value in self._entries.items():
            if value == did:
                return address
        raise ValueError(did)


class _Repo(FakeInboxRepository):
    """The fake repository plus the atomic outbox hand-off the real one performs."""

    def __init__(self, outbox: MailOutbox) -> None:
        super().__init__()
        self._outbox = outbox

    async def record_event_with_outbox(self, **kwargs: object) -> tuple[Any, ...]:
        copies = await super().record_event_with_outbox(**kwargs)  # type: ignore[arg-type]  # reason: test passthrough of the typed seam
        self._outbox.enqueue(str(kwargs["event_id"]), dict(kwargs["envelope"]))  # type: ignore[call-overload]  # reason: envelope is a dict at runtime
        return copies


class _Transport:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def send(self, message: Message) -> Message:
        self.messages.append(message)
        return message


class _Fleet:
    def __init__(self, tmp_path: Path) -> None:
        self.alpha = AgentIdentity.generate("test", "alpha")
        self.beta = AgentIdentity.generate("test", "beta")
        self.operator = AgentIdentity.generate("test", "operator")
        self.outbox = MailOutbox(tmp_path / "outbox.jsonl")
        self.repo = _Repo(self.outbox)
        self.inbox = DurableInboxService(self.repo)
        self.transport = _Transport()
        self.book = _Book(
            {
                "agent://alpha": self.alpha.did,
                "agent://beta": self.beta.did,
                "user://operator": self.operator.did,
            }
        )

    def service(self, identity: AgentIdentity) -> AgentMailService:
        return AgentMailService(
            self.transport,
            self.inbox,
            outbox=self.outbox,
            address_book=self.book,
            signer=MessageSigner.from_identity(identity),
        )

    async def send(self, sender: AgentIdentity, address: str, **extra: object) -> str:
        data: dict[str, object] = {
            "sender": address,
            "sender_did": sender.did,
            "to": ("agent://beta",),
            "subject": "Quarterly numbers",
            "body": "What was Q3 revenue?",
            "idempotency_key": "ask-1",
        }
        data.update(extra)
        result = await self.service(sender).send(MailSendRequest(**data))
        return result.thread_id


def _agent(identity: AgentIdentity) -> Participant:
    return Participant(participant_id=identity.did, role=ParticipantRole.AGENT)


@pytest.fixture
def fleet(tmp_path: Path) -> _Fleet:
    return _Fleet(tmp_path)


def test_mail_target_round_trips_the_conversation() -> None:
    assert mail_target("conversation_1") == "mail://conversation_1"
    assert conversation_of("mail://conversation_1") == "conversation_1"
    assert conversation_of("channel://general") is None
    assert conversation_of(None) is None


async def test_first_reply_is_accepted(fleet: _Fleet) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")

    reply = await fleet.service(fleet.beta).reply_to_conversation(
        conversation, body="Q3 revenue was 4.2M.", idempotency_key="run:1"
    )

    assert reply.body == "Q3 revenue was 4.2M."
    page = await fleet.inbox.list_messages(
        thread_id_for(fleet.alpha.did, conversation), reader=_agent(fleet.alpha)
    )
    assert [item.body for item in page.items] == ["What was Q3 revenue?", "Q3 revenue was 4.2M."]


async def test_reply_to_a_reply_is_refused_and_points_to_the_team_channel(
    fleet: _Fleet,
) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")
    await fleet.service(fleet.beta).reply_to_conversation(
        conversation, body="Q3 revenue was 4.2M.", idempotency_key="run:1"
    )

    with pytest.raises(MailThreadClosedError) as refused:
        await fleet.service(fleet.alpha).reply_to_conversation(
            conversation, body="Thanks! And Q4?", idempotency_key="run:2"
        )

    assert "team channel" in str(refused.value)
    assert "create_task" in str(refused.value)
    page = await fleet.inbox.list_messages(
        thread_id_for(fleet.alpha.did, conversation), reader=_agent(fleet.alpha)
    )
    assert len(page.items) == 2


async def test_a_second_reply_from_the_same_agent_is_refused(fleet: _Fleet) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")
    beta = fleet.service(fleet.beta)
    await beta.reply_to_conversation(conversation, body="first", idempotency_key="run:1")

    with pytest.raises(MailThreadClosedError):
        await beta.reply_to_conversation(conversation, body="second", idempotency_key="run:2")


async def test_replaying_the_same_reply_key_is_idempotent_not_a_second_reply(
    fleet: _Fleet,
) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")
    beta = fleet.service(fleet.beta)
    first = await beta.reply_to_conversation(conversation, body="once", idempotency_key="run:1")

    again = await beta.reply_to_conversation(conversation, body="once", idempotency_key="run:1")

    assert again.message_id == first.message_id
    assert await beta.has_reply(conversation, idempotency_key="run:1") is True
    assert await beta.has_reply(conversation, idempotency_key="run:9") is False


async def test_send_into_a_closed_conversation_is_refused(fleet: _Fleet) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")
    await fleet.service(fleet.beta).reply_to_conversation(
        conversation, body="done", idempotency_key="run:1"
    )

    with pytest.raises(MailThreadClosedError):
        await fleet.send(
            fleet.alpha, "agent://alpha", thread_id=conversation, idempotency_key="ask-2"
        )


async def test_send_addressed_by_did_travels_as_an_agent_handle(fleet: _Fleet) -> None:
    await fleet.send(fleet.alpha, "agent://alpha", to=(fleet.beta.did,))

    assert [message.to for message in fleet.transport.messages] == [["agent://beta"]]
    _, beta_threads, _ = await fleet.inbox.list_threads(_agent(fleet.beta))
    assert len(beta_threads) == 1


async def test_inbound_check_refuses_a_third_message(fleet: _Fleet) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")
    await fleet.service(fleet.beta).reply_to_conversation(
        conversation, body="done", idempotency_key="run:1"
    )
    forged = Message(
        id="message_forged",
        sender="agent://alpha",
        to=["agent://beta"],
        body="one more thing",
        thread_id=conversation,
    )

    with pytest.raises(MailThreadClosedError):
        await fleet.service(fleet.beta).check_inbound(forged)


async def test_inbound_check_admits_the_first_message_and_its_reply(fleet: _Fleet) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")
    page = await fleet.inbox.list_messages(
        thread_id_for(fleet.beta.did, conversation), reader=_agent(fleet.beta)
    )
    first = Message(
        id=page.items[0].event_id or "",
        sender="agent://alpha",
        to=["agent://beta"],
        body="What was Q3 revenue?",
        thread_id=conversation,
    )

    await fleet.service(fleet.beta).check_inbound(first)


async def test_inbound_check_refuses_mail_that_was_never_made_durable(fleet: _Fleet) -> None:
    stray = Message(
        id="message_stray",
        sender="agent://alpha",
        to=["agent://beta"],
        body="out of band",
        thread_id="conversation_unknown",
    )

    with pytest.raises(KeyError):
        await fleet.service(fleet.beta).check_inbound(stray)


async def test_operator_may_answer_an_agent_thread_it_does_not_belong_to(fleet: _Fleet) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")
    beta_thread = thread_id_for(fleet.beta.did, conversation)
    operator = Participant(participant_id=fleet.operator.did, role=ParticipantRole.HUMAN)

    reply = await fleet.service(fleet.operator).reply(
        beta_thread,
        sender=operator,
        body="Use the audited figure.",
        reply_to_id=None,
        idempotency_key="op-1",
        observed_as=_agent(fleet.beta),
    )

    assert reply.sender.participant_id == fleet.operator.did
    page = await fleet.inbox.list_messages(beta_thread, reader=_agent(fleet.beta))
    assert page.items[-1].body == "Use the audited figure."
    _, operator_threads, _ = await fleet.inbox.list_threads(operator)
    assert [item.conversation_id for item in operator_threads] == [conversation]


async def test_an_agent_cannot_join_a_thread_it_does_not_belong_to(fleet: _Fleet) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha", to=("agent://beta",))
    beta_thread = thread_id_for(fleet.beta.did, conversation)
    intruder = AgentIdentity.generate("test", "intruder")

    with pytest.raises(PermissionError):
        await fleet.service(intruder).reply(
            beta_thread,
            sender=_agent(intruder),
            body="let me in",
            reply_to_id=None,
            idempotency_key="x",
            observed_as=_agent(fleet.beta),
        )


async def test_operator_join_still_obeys_the_one_reply_rule(fleet: _Fleet) -> None:
    conversation = await fleet.send(fleet.alpha, "agent://alpha")
    await fleet.service(fleet.beta).reply_to_conversation(
        conversation, body="done", idempotency_key="run:1"
    )
    operator = Participant(participant_id=fleet.operator.did, role=ParticipantRole.HUMAN)

    with pytest.raises(MailThreadClosedError):
        await fleet.service(fleet.operator).reply(
            thread_id_for(fleet.beta.did, conversation),
            sender=operator,
            body="late",
            reply_to_id=None,
            idempotency_key="op-1",
            observed_as=_agent(fleet.beta),
        )


async def test_a_mail_service_never_drains_a_peer_envelope(fleet: _Fleet) -> None:
    """Its transport would re-sign the peer's mail and the recipient would DLQ it."""
    fleet.outbox.enqueue("peer-mail", {"signer_did": fleet.alpha.did, "body": "x"})

    delivered = await fleet.service(fleet.beta).delivery_worker().deliver_once()

    assert delivered == ()
    assert fleet.transport.messages == []
