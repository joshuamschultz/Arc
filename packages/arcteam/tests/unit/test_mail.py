from __future__ import annotations

import pytest

from arcteam.mail import AgentMailService, MailSendRequest


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
