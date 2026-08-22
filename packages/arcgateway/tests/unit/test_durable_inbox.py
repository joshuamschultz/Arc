"""Gateway durable-inbox composition tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository

from arcgateway.executor import Delta, InboundEvent
from arcgateway.inbox import DurableInboxService, participant
from arcgateway.session import SessionRouter


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


class _ReplyingExecutor:
    async def run(self, _event: InboundEvent) -> AsyncIterator[Delta]:
        async def stream() -> AsyncIterator[Delta]:
            yield Delta(kind="token", content="Reviewed.")
            yield Delta(kind="done", is_final=True, turn_id="turn-17")

        return stream()


@pytest.mark.asyncio
async def test_gateway_projects_inbound_and_outbound_once_per_transport_event() -> None:
    repository = FakeInboxRepository()
    router = SessionRouter(_ReplyingExecutor(), inbox_service=DurableInboxService(repository))
    event = InboundEvent(
        platform="web",
        chat_id="chat-1",
        user_did="did:arc:human:alice",
        agent_did="did:arc:agent:bot",
        message="Please review this.",
    )

    await router.handle(event)
    await asyncio.gather(*list(router._pending_tasks))
    await router.handle(event)
    await asyncio.gather(*list(router._pending_tasks))

    agent = participant("did:arc:agent:bot")
    _, threads, _ = await DurableInboxService(repository).list_threads(agent)
    page = await DurableInboxService(repository).list_messages(threads[0].thread_id, reader=agent)
    assert [message.body for message in page.items] == ["Please review this.", "Reviewed."]
    assert len(repository.messages) == 4
