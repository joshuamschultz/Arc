"""Conversation-addressed thread ids and the operator join seam."""

from __future__ import annotations

import pytest

from arcstore.inbox import Participant, ParticipantRole, TraceMetadata
from arcstore.inbox_projection import DurableInboxService, thread_id_for

from .inbox_fake import FakeInboxRepository

_ALPHA = Participant(participant_id="did:arc:test:alpha", role=ParticipantRole.AGENT)
_BETA = Participant(participant_id="did:arc:test:beta", role=ParticipantRole.AGENT)
_OPERATOR = Participant(participant_id="did:arc:test:operator", role=ParticipantRole.HUMAN)


async def _record(
    repo: FakeInboxRepository,
    *,
    event_id: str,
    sender: Participant,
    recipients: tuple[Participant, ...],
    join: bool = False,
) -> None:
    kwargs: dict[str, object] = {
        "event_id": event_id,
        "sender": sender,
        "recipients": recipients,
        "body": event_id,
        "external_thread_id": "conversation-1",
        "subject": "subject",
        "trace": TraceMetadata(),
        "envelope": {},
    }
    if join:
        kwargs["join"] = True
    await repo.record_event_with_outbox(**kwargs)  # type: ignore[arg-type]  # reason: kwargs fan-in


async def test_thread_id_for_names_the_owner_copy_of_a_conversation() -> None:
    repo = FakeInboxRepository()
    service = DurableInboxService(repo)
    await _record(repo, event_id="e1", sender=_ALPHA, recipients=(_BETA,))

    _, threads, _ = await service.list_threads(_BETA)

    assert threads[0].thread_id == thread_id_for(_BETA.participant_id, "conversation-1")


async def test_a_new_participant_is_refused_without_an_explicit_join() -> None:
    repo = FakeInboxRepository()
    await _record(repo, event_id="e1", sender=_ALPHA, recipients=(_BETA,))

    with pytest.raises(ValueError, match="different participants"):
        await _record(repo, event_id="e2", sender=_OPERATOR, recipients=(_ALPHA, _BETA))


async def test_an_explicit_join_adds_the_participant_to_every_copy() -> None:
    repo = FakeInboxRepository()
    service = DurableInboxService(repo)
    await _record(repo, event_id="e1", sender=_ALPHA, recipients=(_BETA,))

    await _record(repo, event_id="e2", sender=_OPERATOR, recipients=(_ALPHA, _BETA), join=True)

    for owner in (_ALPHA, _BETA, _OPERATOR):
        thread = await service.get_thread(
            thread_id_for(owner.participant_id, "conversation-1"),
            reader_id=owner.participant_id,
        )
        assert {item.participant_id for item in thread.participants} == {
            _ALPHA.participant_id,
            _BETA.participant_id,
            _OPERATOR.participant_id,
        }


async def test_a_join_can_never_remove_a_participant() -> None:
    repo = FakeInboxRepository()
    await _record(repo, event_id="e1", sender=_ALPHA, recipients=(_BETA,))

    with pytest.raises(ValueError, match="different participants"):
        await _record(repo, event_id="e2", sender=_OPERATOR, recipients=(_ALPHA,), join=True)
