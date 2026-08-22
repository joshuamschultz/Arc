from __future__ import annotations

import pytest
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository

from arcstore.inbox import (
    Handoff,
    Inbox,
    InboxRepository,
    Message,
    Participant,
    ReadState,
    Thread,
    TraceMetadata,
)


def _participant(identifier: str, role: str = "agent") -> Participant:
    return Participant(participant_id=identifier, role=role, display_name=identifier)


def test_contracts_have_stable_ids_and_sessions_are_trace_metadata() -> None:
    owner = _participant("did:arc:human:operator", "human")
    inbox = Inbox(owner=owner)
    thread = Thread(
        inbox_id=inbox.inbox_id, participants=(owner, _participant("did:arc:agent:terra"))
    )
    trace = TraceMetadata(source_session_id="session-2026-08-22", classification="unclassified")
    message = Message(
        thread_id=thread.thread_id,
        sender=_participant("did:arc:agent:terra"),
        recipients=(owner,),
        body="handoff ready",
        trace=trace,
    )
    assert inbox.inbox_id
    assert thread.thread_id
    assert message.message_id
    assert message.reply_to_id is None
    assert message.state_for(owner.participant_id) is ReadState.UNREAD
    assert message.trace.classification == "UNCLASSIFIED"
    assert "session" not in thread.model_dump()


def test_classification_metadata_is_closed_and_normalized() -> None:
    trace = TraceMetadata(trace_id="trace-1", classification="SECRET")
    assert trace.classification == "SECRET"
    with pytest.raises(ValueError, match="unknown classification"):
        TraceMetadata(classification="private")


def test_participant_and_recipient_ids_are_unique() -> None:
    owner = _participant("did:arc:human:operator", "human")
    with pytest.raises(ValueError, match="participants"):
        Thread(inbox_id="inbox-1", participants=(owner, owner))
    with pytest.raises(ValueError, match="recipients"):
        Message(
            thread_id="thread-1",
            sender=owner,
            recipients=(owner, owner),
            body="duplicate recipient",
        )


def test_handoff_contains_trace_links_without_free_form_metadata() -> None:
    handoff = Handoff(
        thread_id="thread-1",
        from_participant=_participant("did:arc:agent:terra"),
        to_participants=(_participant("did:arc:agent:luna"),),
        source_message_id="message-1",
        trace=TraceMetadata(trace_id="trace-1", handoff_id="handoff-1"),
    )
    assert handoff.handoff_id
    assert handoff.trace.handoff_id == "handoff-1"
    assert not hasattr(handoff, "metadata")


@pytest.mark.asyncio
async def test_inbox_repository_tracks_explicit_read_state_and_reply_chain() -> None:
    repository: InboxRepository = FakeInboxRepository()
    owner = _participant("did:arc:human:operator", "human")
    agent = _participant("did:arc:agent:terra")
    inbox = await repository.create_inbox(owner, classification="CUI")
    thread = await repository.create_thread(inbox.inbox_id, (owner, agent), subject="Release")
    first = await repository.append_message(
        thread.thread_id,
        sender=owner,
        recipients=(agent,),
        body="Can you review this?",
    )
    reply = await repository.append_message(
        thread.thread_id,
        sender=agent,
        recipients=(owner,),
        body="Reviewed.",
        reply_to_id=first.message_id,
    )
    assert reply.reply_to_id == first.message_id
    assert (
        await repository.get_thread(thread.thread_id, reader_id=owner.participant_id)
    ).unread_count == 1
    await repository.mark_read(reply.message_id, owner.participant_id)
    assert (
        await repository.get_thread(thread.thread_id, reader_id=owner.participant_id)
    ).unread_count == 0
    visible = await repository.list_messages(
        thread.thread_id,
        reader_id=owner.participant_id,
        classification_max="CUI",
    )
    assert {message.message_id for message in visible.items} == {
        first.message_id,
        reply.message_id,
    }


@pytest.mark.asyncio
async def test_cursor_pagination_is_stable_and_opaque() -> None:
    repository = FakeInboxRepository()
    owner = _participant("did:arc:human:operator", "human")
    inbox = await repository.create_inbox(owner)
    for subject in ("one", "two", "three"):
        await repository.create_thread(inbox.inbox_id, (owner,), subject=subject)

    first = await repository.list_threads(inbox.inbox_id, reader_id=owner.participant_id, limit=2)
    assert len(first.items) == 2
    assert first.page_info.has_more
    assert first.page_info.next_cursor
    second = await repository.list_threads(
        inbox.inbox_id,
        reader_id=owner.participant_id,
        limit=2,
        cursor=first.page_info.next_cursor,
    )
    assert len(second.items) == 1
    assert not ({t.thread_id for t in first.items} & {t.thread_id for t in second.items})
    assert (
        await repository.list_threads(
            inbox.inbox_id,
            reader_id=owner.participant_id,
            limit=2,
            cursor=first.page_info.next_cursor,
        )
        == second
    )
    repository.threads.pop(first.items[-1].thread_id)
    with pytest.raises(ValueError, match="no longer references"):
        await repository.list_threads(
            inbox.inbox_id,
            reader_id=owner.participant_id,
            limit=2,
            cursor=first.page_info.next_cursor,
        )


@pytest.mark.asyncio
async def test_repository_filters_up_classification_fail_closed() -> None:
    repository = FakeInboxRepository()
    owner = _participant("did:arc:human:operator", "human")
    inbox = await repository.create_inbox(owner, classification="SECRET")
    await repository.create_thread(
        inbox.inbox_id, (owner,), subject="classified", classification="SECRET"
    )
    page = await repository.list_threads(
        inbox.inbox_id,
        reader_id=owner.participant_id,
        classification_max="UNCLASSIFIED",
    )
    assert page.items == ()
    with pytest.raises(ValueError, match="unknown classification"):
        await repository.list_threads(
            inbox.inbox_id,
            reader_id=owner.participant_id,
            classification_max="private",
        )


@pytest.mark.asyncio
async def test_thread_reads_require_participation_and_clearance() -> None:
    repository = FakeInboxRepository()
    owner = _participant("did:arc:human:operator", "human")
    outsider = _participant("did:arc:human:outsider", "human")
    inbox = await repository.create_inbox(owner, classification="SECRET")
    thread = await repository.create_thread(inbox.inbox_id, (owner,), classification="SECRET")
    with pytest.raises(PermissionError, match="participate"):
        await repository.get_thread(
            thread.thread_id, reader_id=outsider.participant_id, classification_max="SECRET"
        )
    with pytest.raises(PermissionError, match="clearance"):
        await repository.get_thread(thread.thread_id, reader_id=owner.participant_id)
    with pytest.raises(PermissionError, match="own the inbox"):
        await repository.list_threads(
            inbox.inbox_id,
            reader_id=outsider.participant_id,
            classification_max="SECRET",
        )
    with pytest.raises(PermissionError, match="participate"):
        await repository.list_messages(
            thread.thread_id,
            reader_id=outsider.participant_id,
            classification_max="SECRET",
        )


@pytest.mark.asyncio
async def test_repository_contract_is_storage_neutral() -> None:
    assert not hasattr(InboxRepository, "sqlite")
    repository = FakeInboxRepository()
    owner = _participant("did:arc:human:operator", "human")
    inbox = await repository.create_inbox(owner, classification="CUI")
    thread = await repository.create_thread(inbox.inbox_id, (owner,), classification="CUI")
    with pytest.raises(ValueError, match="match thread"):
        await repository.append_message(
            thread.thread_id,
            sender=owner,
            recipients=(owner,),
            body="mismatched classification",
            trace=TraceMetadata(classification="SECRET"),
        )
    await repository.create_handoff(
        thread.thread_id,
        from_participant=owner,
        to_participants=(owner,),
        source_message_id=None,
        trace=TraceMetadata(classification="CUI"),
    )
    handoffs = await repository.list_handoffs(
        thread.thread_id, reader_id=owner.participant_id, classification_max="CUI"
    )
    assert len(handoffs) == 1
