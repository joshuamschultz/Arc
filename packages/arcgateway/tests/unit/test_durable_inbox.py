"""Gateway durable-inbox composition tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from arcstore.inbox_spool import InboxProjectionSpool
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


class _FailingRepository(FakeInboxRepository):
    async def create_inbox(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise ConnectionError("inbox backend down")


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


@pytest.mark.asyncio
async def test_gateway_projection_outage_is_durable_and_retries_after_restart(
    tmp_path: Path,
) -> None:
    spool = InboxProjectionSpool(tmp_path / "gateway-inbox.jsonl")
    service = DurableInboxService(_FailingRepository(), projection_spool=spool)
    router = SessionRouter(_ReplyingExecutor(), inbox_service=service)
    event = InboundEvent(
        platform="web",
        chat_id="chat-1",
        user_did="did:arc:human:alice",
        agent_did="did:arc:agent:bot",
        message="Please review this.",
    )
    await router.handle(event)
    await asyncio.gather(*list(router._pending_tasks))
    assert len(spool.pending()) == 2

    repository = FakeInboxRepository()
    restarted = DurableInboxService(repository, projection_spool=spool)
    assert len(await restarted.retry_pending_projections()) == 2
    assert len(repository.messages) == 4
