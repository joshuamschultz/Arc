"""The handoff delivery port: a durable handoff wakes its recipient by signed mail."""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.inbox import HandoffStatus, Participant, ParticipantRole, TraceMetadata
from arcstore.inbox_projection import DurableInboxService, InboxDeliveryPort, thread_id_for
from arcstore.mail_outbox import MailOutbox
from arctrust import AgentIdentity
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository

from arcteam.crypto import MessageSigner
from arcteam.mail import AgentMailService, MailInboxDeliveryPort, MailSendRequest
from arcteam.types import Message


class _Book:
    def __init__(self, entries: dict[str, str]) -> None:
        self._entries = entries

    async def did_for(self, address: str) -> str:
        return address if address.startswith("did:") else self._entries[address]

    async def address_for(self, did: str) -> str:
        return next(address for address, value in self._entries.items() if value == did)


class _Transport:
    async def send(self, message: Message) -> Message:
        return message


@pytest.fixture
def operator() -> AgentIdentity:
    return AgentIdentity.generate("test", "operator")


@pytest.fixture
def beta() -> AgentIdentity:
    return AgentIdentity.generate("test", "beta")


def _compose(
    tmp_path: Path, operator: AgentIdentity, beta: AgentIdentity
) -> tuple[AgentMailService, DurableInboxService, MailInboxDeliveryPort]:
    port = MailInboxDeliveryPort()
    inbox = DurableInboxService(FakeInboxRepository(), delivery_port=port)
    mail = AgentMailService(
        _Transport(),
        inbox,
        outbox=MailOutbox(tmp_path / "outbox.jsonl"),
        address_book=_Book({"user://operator": operator.did, "agent://beta": beta.did}),
        signer=MessageSigner.from_identity(operator),
    )
    port.bind(mail)
    return mail, inbox, port


def test_the_port_satisfies_the_inbox_delivery_contract() -> None:
    assert isinstance(MailInboxDeliveryPort(), InboxDeliveryPort)


async def test_an_unbound_port_fails_closed_as_unavailable() -> None:
    from arcstore.inbox import Handoff

    handoff = Handoff(
        thread_id="t",
        from_participant=Participant(participant_id="did:a", role=ParticipantRole.HUMAN),
        to_participants=(Participant(participant_id="did:b", role=ParticipantRole.AGENT),),
        trace=TraceMetadata(),
    )

    with pytest.raises(RuntimeError, match="unavailable"):
        await MailInboxDeliveryPort().wake_handoff(handoff)


async def test_handoff_round_trip_wakes_the_recipient_and_records_the_resolution(
    tmp_path: Path, operator: AgentIdentity, beta: AgentIdentity
) -> None:
    mail, inbox, _port = _compose(tmp_path, operator, beta)
    sent = await mail.send(
        MailSendRequest(
            sender="user://operator",
            sender_did=operator.did,
            to=("agent://beta",),
            subject="Own the Q3 close",
            body="Please take the Q3 close.",
            idempotency_key="compose-1",
        )
    )
    op = Participant(participant_id=operator.did, role=ParticipantRole.HUMAN)
    agent = Participant(participant_id=beta.did, role=ParticipantRole.AGENT)

    handoff = await inbox.create_handoff(
        thread_id_for(beta.did, sent.thread_id),
        sender=op,
        recipients=(agent,),
        source_message_id=None,
        trace=TraceMetadata(),
        idempotency_key="handoff-1",
    )

    _, threads, _ = await inbox.list_threads(agent)
    subjects = {thread.subject for thread in threads}
    assert "Handoff: Own the Q3 close" in subjects
    resolved = await inbox.resolve_handoff(
        handoff.handoff_id, recipient=agent, actor_did=operator.did, status=HandoffStatus.ACCEPTED
    )
    assert resolved.status is HandoffStatus.ACCEPTED
