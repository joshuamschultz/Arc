from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.inbox import Participant, ParticipantRole, Thread
from arcstore.inbox_projection import DurableInboxService
from arcstore.mail_outbox import MailOutbox
from arctrust import AgentIdentity
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository

from arcteam.crypto import MessageSigner, verify_message
from arcteam.mail import AgentMailService, MailDeliveryWorker, MailSendRequest
from arcteam.types import Message


class _AddressBook:
    def __init__(self, identity: AgentIdentity) -> None:
        self._addresses = {
            "agent://alpha": identity.did,
            "agent://beta": "did:arc:local:agent/beta",
        }

    async def did_for(self, address: str) -> str:
        return self._addresses[address]

    async def address_for(self, did: str) -> str:
        for address, resolved in self._addresses.items():
            if resolved == did:
                return address
        raise ValueError(did)


class _Store:
    def __init__(self, outbox: MailOutbox) -> None:
        self.events: list[dict[str, object]] = []
        self._outbox = outbox

    async def record_event_with_outbox(self, **kwargs: object) -> tuple[object, ...]:
        self.events.append(kwargs)
        self._outbox.enqueue(str(kwargs["event_id"]), dict(kwargs["envelope"]))
        return (object(),)


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[Message] = []

    async def send(self, message: Message) -> Message:
        self.messages.append(message)
        if self.fail:
            raise ConnectionError("nats unavailable")
        return message


def _identity() -> AgentIdentity:
    return AgentIdentity.generate("test", "mail-sender")


def _request(identity: AgentIdentity, **overrides: object) -> MailSendRequest:
    data: dict[str, object] = {
        "sender": "agent://alpha",
        "sender_did": identity.did,
        "to": ("agent://beta",),
        "subject": "handoff",
        "body": "Please review.",
        "idempotency_key": "mail-1",
    }
    data.update(overrides)
    return MailSendRequest(**data)


def _service(
    tmp_path: Path, identity: AgentIdentity, transport: _Transport
) -> tuple[AgentMailService, _Store, MailOutbox]:
    outbox = MailOutbox(tmp_path / "mail-outbox.jsonl")
    store = _Store(outbox)
    return (
        AgentMailService(
            transport,
            store,
            outbox=outbox,
            address_book=_AddressBook(identity),
            signer=MessageSigner.from_identity(identity),
        ),
        store,
        outbox,
    )


@pytest.mark.asyncio
async def test_mail_persists_canonical_dids_before_transport(tmp_path: Path) -> None:
    identity = _identity()
    transport = _Transport(fail=True)
    service, store, _outbox = _service(tmp_path, identity, transport)

    result = await service.send(_request(identity))

    assert result.status == "pending"
    assert store.events[0]["sender"].participant_id == identity.did
    assert store.events[0]["recipients"][0].participant_id == "did:arc:local:agent/beta"
    assert transport.messages[0].to == ["agent://beta"]


@pytest.mark.asyncio
async def test_mail_rejects_sender_did_that_does_not_own_the_signing_key(tmp_path: Path) -> None:
    identity = _identity()
    service, _store, _outbox = _service(tmp_path, identity, _Transport())

    with pytest.raises(PermissionError, match="configured signer"):
        await service.send(_request(identity, sender_did="did:arc:local:agent/imposter"))


@pytest.mark.asyncio
async def test_mail_is_signed_before_it_enters_the_durable_outbox(tmp_path: Path) -> None:
    identity = _identity()
    service, _store, outbox = _service(tmp_path, identity, _Transport(fail=True))

    await service.send(_request(identity))

    envelope = Message.model_validate(outbox.pending()[0].envelope)
    assert envelope.signer_did == identity.did
    assert verify_message(envelope, identity.public_key) is True


class _LeaseLossOutbox:
    def __init__(self) -> None:
        self._entry = type(
            "Entry",
            (),
            {
                "event_id": "mail-1",
                "attempts": 1,
                "envelope": {"sender": "a", "to": ["agent://b"], "body": "x"},
            },
        )()

    def claim(self, _worker_id: str, *, limit: int) -> tuple[Any, ...]:
        return (self._entry,)

    def ack(self, _worker_id: str, _event_id: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_delivery_does_not_report_sent_when_outbox_ack_loses_its_lease() -> None:
    transport = _Transport()

    delivered = await MailDeliveryWorker(
        _LeaseLossOutbox(), transport, worker_id="worker"
    ).deliver_once()

    assert delivered == ()
    assert len(transport.messages) == 1


class _DeadLetterOutbox:
    def __init__(self) -> None:
        self.entry = type(
            "Entry",
            (),
            {
                "event_id": "mail-terminal",
                "attempts": 5,
                "envelope": {"sender": "a", "to": ["agent://b"], "body": "x"},
            },
        )()
        self.reason: str | None = None

    def claim(self, _worker_id: str, *, limit: int) -> tuple[Any, ...]:
        return (self.entry,)

    def dead_letter(self, _worker_id: str, _event_id: str, *, reason: str) -> bool:
        self.reason = reason
        return True


@pytest.mark.asyncio
async def test_delivery_dead_letters_after_the_bounded_attempt_limit() -> None:
    outbox = _DeadLetterOutbox()
    notified: list[str] = []

    async def notify(entry: Any, _error: Exception) -> None:
        notified.append(entry.event_id)

    await MailDeliveryWorker(
        outbox,
        _Transport(fail=True),
        worker_id="worker",
        max_attempts=5,
        on_dead_letter=notify,
    ).deliver_once()

    assert outbox.reason == "ConnectionError"
    assert notified == ["mail-terminal"]


class _ReplyStore(_Store):
    def __init__(self, outbox: MailOutbox, sender: str) -> None:
        super().__init__(outbox)
        self._thread = Thread(
            thread_id="thread-existing",
            conversation_id="conversation-existing",
            inbox_id="inbox-sender",
            participants=(
                Participant(participant_id=sender, role=ParticipantRole.AGENT),
                Participant(participant_id="did:arc:local:agent/beta", role=ParticipantRole.AGENT),
            ),
            subject="handoff",
        )

    async def get_thread(self, _thread_id: str, **_kwargs: object) -> Thread:
        return self._thread

    async def record_event_with_outbox(self, **kwargs: object) -> tuple[object, ...]:
        await super().record_event_with_outbox(**kwargs)
        return ("durable-reply",)


@pytest.mark.asyncio
async def test_reply_uses_the_atomic_outbox_instead_of_the_inbox_delivery_port(
    tmp_path: Path,
) -> None:
    identity = _identity()
    outbox = MailOutbox(tmp_path / "mail-outbox.jsonl")
    store = _ReplyStore(outbox, identity.did)
    service = AgentMailService(
        _Transport(fail=True),
        store,
        outbox=outbox,
        address_book=_AddressBook(identity),
        signer=MessageSigner.from_identity(identity),
    )

    reply = await service.reply(
        "thread-existing",
        sender=Participant(participant_id=identity.did, role=ParticipantRole.AGENT),
        body="done",
        reply_to_id=None,
        idempotency_key="reply-1",
    )

    assert reply == "durable-reply"
    assert store.events[-1]["external_thread_id"] == "conversation-existing"
    assert len(outbox.pending()) == 1


@pytest.mark.asyncio
async def test_reply_translates_another_mailbox_local_ids_to_canonical_mail_identity(
    tmp_path: Path,
) -> None:
    identity = _identity()
    beta_did = "did:arc:local:agent/beta"
    outbox = MailOutbox(tmp_path / "mail-outbox.jsonl")
    durable = DurableInboxService(FakeInboxRepository())
    service = AgentMailService(
        _Transport(),
        durable,
        outbox=outbox,
        address_book=_AddressBook(identity),
        signer=MessageSigner.from_identity(identity),
    )
    await service.send(_request(identity))
    operator = Participant(participant_id=identity.did, role=ParticipantRole.AGENT)
    beta = Participant(participant_id=beta_did, role=ParticipantRole.AGENT)
    _, beta_threads, _ = await durable.list_threads(beta)
    beta_thread = beta_threads[0]
    beta_messages = await durable.list_messages(beta_thread.thread_id, reader=beta)
    parent = beta_messages.items[0]

    reply = await service.reply(
        beta_thread.thread_id,
        sender=operator,
        body="review complete",
        reply_to_id=parent.message_id,
        idempotency_key="reply-from-operator",
    )

    _, operator_threads, _ = await durable.list_threads(operator)
    operator_thread = operator_threads[0]
    assert beta_thread.thread_id != operator_thread.thread_id
    assert beta_thread.conversation_id == operator_thread.conversation_id
    assert reply.thread_id == operator_thread.thread_id
    operator_messages = await durable.list_messages(operator_thread.thread_id, reader=operator)
    beta_messages = await durable.list_messages(beta_thread.thread_id, reader=beta)
    assert operator_messages.items[-1].reply_to_id == operator_messages.items[0].message_id
    assert beta_messages.items[-1].reply_to_id == beta_messages.items[0].message_id
    assert operator_messages.items[-1].reply_to_event_id == parent.event_id
    assert beta_messages.items[-1].reply_to_event_id == parent.event_id
