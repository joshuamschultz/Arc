"""Reusable behavioral checks for every :class:`InboxRepository` implementation."""

from __future__ import annotations

import pytest

from arcstore.inbox import InboxRepository, Participant, TraceMetadata


def participant(identifier: str, role: str = "agent") -> Participant:
    return Participant(participant_id=identifier, role=role, display_name=identifier)


async def assert_inbox_repository_conforms(repository: InboxRepository) -> None:
    """Exercise storage-independent inbox guarantees against a repository."""
    owner = participant("did:arc:human:owner", "human")
    agent = participant("did:arc:agent:writer")
    outsider = participant("did:arc:human:outsider", "human")
    inbox = await repository.create_inbox(owner, classification="CUI")
    thread = await repository.create_thread(inbox.inbox_id, (owner, agent), subject="status")
    first = await repository.append_message(
        thread.thread_id, sender=owner, recipients=(agent,), body="Please review."
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
    updated = await repository.mark_read(reply.message_id, owner.participant_id)
    assert updated.state_for(owner.participant_id).value == "read"
    assert (
        await repository.get_thread(thread.thread_id, reader_id=owner.participant_id)
    ).unread_count == 0
    messages = await repository.list_messages(
        thread.thread_id, reader_id=owner.participant_id, classification_max="CUI"
    )
    assert {item.message_id for item in messages.items} == {first.message_id, reply.message_id}
    with pytest.raises(PermissionError, match="participate"):
        await repository.get_thread(
            thread.thread_id, reader_id=outsider.participant_id, classification_max="CUI"
        )

    await repository.create_handoff(
        thread.thread_id,
        from_participant=owner,
        to_participants=(agent,),
        source_message_id=reply.message_id,
        trace=TraceMetadata(classification=thread.classification, trace_id="trace-1"),
    )
    assert (
        len(
            await repository.list_handoffs(
                thread.thread_id, reader_id=agent.participant_id, classification_max="CUI"
            )
        )
        == 1
    )

    for subject in ("one", "two", "three"):
        await repository.create_thread(inbox.inbox_id, (owner,), subject=subject)
    page = await repository.list_threads(inbox.inbox_id, reader_id=owner.participant_id, limit=2)
    assert len(page.items) == 2 and page.page_info.has_more and page.page_info.next_cursor
    next_page = await repository.list_threads(
        inbox.inbox_id, reader_id=owner.participant_id, limit=2, cursor=page.page_info.next_cursor
    )
    assert not (
        {item.thread_id for item in page.items} & {item.thread_id for item in next_page.items}
    )
    with pytest.raises(ValueError, match=r"invalid cursor|scope"):
        await repository.list_threads(
            inbox.inbox_id, reader_id=owner.participant_id, cursor="not-a-cursor"
        )
