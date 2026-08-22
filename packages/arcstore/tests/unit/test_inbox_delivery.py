"""Durable inbox effects: outage replay, idempotent reply, handoff lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository

from arcstore.inbox import Handoff, HandoffStatus, Message, ParticipantRole, TraceMetadata
from arcstore.inbox_projection import DurableInboxService, participant
from arcstore.inbox_spool import InboxProjectionSpool


class _Port:
    def __init__(self) -> None:
        self.replies: list[Message] = []
        self.wakes: list[Handoff] = []
        self.resolutions: list[Handoff] = []
        self.fail_replies = False

    async def deliver_reply(self, message: Message) -> None:
        if self.fail_replies:
            raise ConnectionError("recipient transport is down")
        self.replies.append(message)

    async def wake_handoff(self, handoff: Handoff) -> None:
        self.wakes.append(handoff)

    async def deliver_handoff_resolution(self, handoff: Handoff) -> None:
        self.resolutions.append(handoff)


class _FailingRepository(FakeInboxRepository):
    async def create_inbox(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise ConnectionError("database is down")


@pytest.mark.asyncio
async def test_projection_outage_is_spooled_then_replayed_once_after_restart(
    tmp_path: Path,
) -> None:
    spool = InboxProjectionSpool(tmp_path / "inbox-projection.jsonl")
    sender = participant("did:arc:human:alice", role=ParticipantRole.HUMAN)
    recipient = participant("did:arc:agent:bot")
    down = DurableInboxService(_FailingRepository(), projection_spool=spool)

    with pytest.raises(ConnectionError, match="database is down"):
        await down.record_event(
            event_id="gateway:17", sender=sender, recipients=(recipient,), body="status?"
        )
    assert [event.event_id for event in spool.pending()] == ["gateway:17"]

    repository = FakeInboxRepository()
    restarted = DurableInboxService(
        repository, projection_spool=InboxProjectionSpool(tmp_path / "inbox-projection.jsonl")
    )
    assert await restarted.retry_pending_projections() == ("gateway:17",)
    assert await restarted.retry_pending_projections() == ()
    assert len(repository.messages) == 2


@pytest.mark.asyncio
async def test_reply_persists_before_outage_and_retries_with_one_deterministic_delivery() -> None:
    repository = FakeInboxRepository()
    port = _Port()
    service = DurableInboxService(repository, delivery_port=port)
    human = participant("did:arc:human:alice", role=ParticipantRole.HUMAN)
    agent = participant("did:arc:agent:bot")
    await service.record_event(
        event_id="event-1", sender=human, recipients=(agent,), body="review"
    )
    _, threads, _ = await service.list_threads(agent)

    port.fail_replies = True
    with pytest.raises(ConnectionError):
        await service.reply(
            threads[0].thread_id, sender=agent, body="done", idempotency_key="reply-1"
        )
    assert len(repository.messages) == 3

    port.fail_replies = False
    retried = await service.reply(
        threads[0].thread_id, sender=agent, body="done", idempotency_key="reply-1"
    )
    assert len(repository.messages) == 3
    assert [message.message_id for message in port.replies] == [retried.message_id]


@pytest.mark.asyncio
async def test_handoff_is_woken_then_only_recipient_can_resolve_idempotently() -> None:
    repository = FakeInboxRepository()
    port = _Port()
    service = DurableInboxService(repository, delivery_port=port)
    operator = participant("did:arc:agent:operator")
    recipient = participant("did:arc:agent:recipient")
    await service.record_event(
        event_id="event-1", sender=operator, recipients=(recipient,), body="please take this"
    )
    _, threads, _ = await service.list_threads(recipient)
    handoff = await service.create_handoff(
        threads[0].thread_id,
        sender=operator,
        recipients=(recipient,),
        source_message_id=None,
        trace=TraceMetadata(),
        idempotency_key="handoff-1",
    )
    assert port.wakes == [handoff]
    with pytest.raises(PermissionError):
        await service.resolve_handoff(
            handoff.handoff_id,
            recipient=operator,
            status=HandoffStatus.ACCEPTED,
        )
    accepted = await service.resolve_handoff(
        handoff.handoff_id, recipient=recipient, status=HandoffStatus.ACCEPTED
    )
    assert accepted.status is HandoffStatus.ACCEPTED
    assert accepted.resolved_by == recipient
    assert (
        await service.resolve_handoff(
            handoff.handoff_id, recipient=recipient, status=HandoffStatus.ACCEPTED
        )
        == accepted
    )
    assert port.resolutions == [accepted, accepted]
