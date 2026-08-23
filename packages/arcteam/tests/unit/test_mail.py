from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.mail_outbox import MailOutbox
from arctrust import AgentIdentity

from arcteam.crypto import MessageSigner, verify_message
from arcteam.mail import AgentMailService, MailSendRequest
from arcteam.types import Message


class _Store:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def record_event(self, **kwargs: object) -> tuple[object, ...]:
        self.events.append(kwargs)
        return ()


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[object] = []

    async def send(self, message: object) -> object:
        self.messages.append(message)
        if self.fail:
            raise ConnectionError("nats unavailable")
        return message


def _request(**overrides: object) -> MailSendRequest:
    data: dict[str, object] = {
        "sender": "agent://alpha",
        "to": ("agent://beta",),
        "subject": "handoff",
        "body": "Please review.",
        "idempotency_key": "mail-1",
    }
    data.update(overrides)
    return MailSendRequest(**data)


@pytest.mark.asyncio
async def test_mail_persists_before_transport_and_marks_pending_on_outage() -> None:
    store = _Store()
    transport = _Transport(fail=True)
    result = await AgentMailService(transport, store).send(_request())

    assert result.status == "pending"
    assert len(store.events) == 1
    assert len(transport.messages) == 1
    assert store.events[0]["external_thread_id"] == result.thread_id


@pytest.mark.asyncio
async def test_mail_rejects_bcc_until_durable_privacy_is_available() -> None:
    with pytest.raises(ValueError, match="bcc"):
        await AgentMailService(_Transport(), _Store()).send(
            _request(bcc=("agent://secret",))
        )


@pytest.mark.asyncio
async def test_mail_outbox_replays_after_transport_restart(tmp_path: Path) -> None:
    store = _Store()
    outbox = MailOutbox(tmp_path / "mail-outbox.jsonl")
    down = _Transport(fail=True)
    pending = await AgentMailService(down, store, outbox=outbox).send(_request())
    assert pending.status == "pending"
    assert len(outbox.pending()) == 1
    assert outbox.nack("arc-team-mail", pending.message_id, retry_after_seconds=0) is True

    up = _Transport()
    replayed = await AgentMailService(up, store, outbox=MailOutbox(tmp_path / "mail-outbox.jsonl")).send(
        _request(idempotency_key="mail-2")
    )
    assert replayed.status == "sent"
    assert [getattr(item, "id", None) for item in up.messages] == [
        pending.message_id,
        replayed.message_id,
    ]


@pytest.mark.asyncio
async def test_mail_is_signed_before_it_enters_the_durable_outbox(tmp_path: Path) -> None:
    identity = AgentIdentity.generate("test", "mail-sender")
    outbox = MailOutbox(tmp_path / "mail-outbox.jsonl")
    await AgentMailService(
        _Transport(fail=True),
        _Store(),
        outbox=outbox,
        signer=MessageSigner.from_identity(identity),
    ).send(_request(sender=identity.did))

    envelope = Message.model_validate(outbox.pending()[0].envelope)
    assert envelope.signer_did == identity.did
    assert verify_message(envelope, identity.public_key) is True
