"""Gateway durable-inbox composition tests."""

from __future__ import annotations

import pytest
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository

from arcgateway.inbox import DurableInboxService, participant


@pytest.mark.asyncio
async def test_event_projection_is_idempotent_and_keeps_separate_read_copies() -> None:
    repository = FakeInboxRepository()
    service = DurableInboxService(repository)
    alice = participant("did:arc:human:alice")
    bot = participant("did:arc:agent:bot")

    first = await service.record_event(
        event_id="bus-17", sender=alice, recipients=(bot,), body="Status?"
    )
    retry = await service.record_event(
        event_id="bus-17", sender=alice, recipients=(bot,), body="Status?"
    )

    assert len(repository.messages) == 2
    assert tuple(message.message_id for message in retry) == tuple(
        message.message_id for message in first
    )
    bot_copy = next(message for message in first if message.recipients == (bot,))
    await repository.mark_read(bot_copy.message_id, bot.participant_id)
    assert repository.messages[bot_copy.message_id].state_for(bot.participant_id).value == "read"
