"""PostgreSQL implementation of the storage-neutral inbox repository."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Any, TypeVar

from arctrust.classification import Classification, dominates, parse_classification
from pydantic import BaseModel

from arcstore.backends.postgres import PostgresBackend, _as_dict, _json
from arcstore.inbox import (
    Handoff,
    HandoffStatus,
    Inbox,
    Message,
    MessagePage,
    PageInfo,
    Participant,
    ReadReceipt,
    ReadState,
    Thread,
    ThreadPage,
    TraceMetadata,
    _id,
)

_MAX_PAGE_SIZE = 100
_Model = TypeVar("_Model", bound=BaseModel)


class PostgresInboxRepository:
    """Durable inbox adapter using the already-started :class:`PostgresBackend` pool.

    V1 keeps the canonical Pydantic JSON document in each table's ``payload``
    column.  The relational ``inbox_id``/``thread_id`` columns remain the source
    of foreign-key integrity and query scope; they are deliberately not inferred
    from JSON.
    """

    def __init__(self, backend: PostgresBackend) -> None:
        self._backend = backend

    async def create_inbox(
        self,
        owner: Participant,
        *,
        classification: str = "UNCLASSIFIED",
        inbox_id: str | None = None,
    ) -> Inbox:
        inbox = Inbox(
            owner=owner,
            classification=classification,
            inbox_id=inbox_id or _id("inbox"),
        )
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                "INSERT INTO inboxes(inbox_id, payload, created_at) VALUES ($1, $2::jsonb, $3) "
                "ON CONFLICT(inbox_id) DO NOTHING",
                inbox.inbox_id,
                _model_json(inbox),
                inbox.created_at,
            )
            if not str(result).endswith("1"):
                return await self._get_inbox(connection, inbox.inbox_id)
        return inbox

    async def get_inbox(self, inbox_id: str) -> Inbox:
        async with self._pool.acquire() as connection:
            return await self._get_inbox(connection, inbox_id)

    async def create_thread(
        self,
        inbox_id: str,
        participants: tuple[Participant, ...],
        *,
        subject: str | None = None,
        classification: str = "UNCLASSIFIED",
        thread_id: str | None = None,
    ) -> Thread:
        thread = Thread(
            inbox_id=inbox_id,
            participants=participants,
            subject=subject,
            classification=classification,
            thread_id=thread_id or _id("thread"),
        )
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                inbox = await self._get_inbox(connection, inbox_id)
                if not dominates(_level(inbox.classification), _level(thread.classification)):
                    raise ValueError("thread classification exceeds inbox classification")
                result = await connection.execute(
                    "INSERT INTO inbox_threads(thread_id, inbox_id, payload, updated_at) "
                    "VALUES ($1, $2, $3::jsonb, $4) "
                    "ON CONFLICT(thread_id) DO NOTHING",
                    thread.thread_id,
                    thread.inbox_id,
                    _model_json(thread),
                    thread.updated_at,
                )
                if not str(result).endswith("1"):
                    return await self._get_thread(connection, thread.thread_id)
        return thread

    async def get_thread(
        self,
        thread_id: str,
        *,
        reader_id: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Thread:
        clearance = _level(classification_max)
        async with self._pool.acquire() as connection:
            thread = await self._get_thread(connection, thread_id)
            _require_participant(thread, reader_id)
            if not dominates(clearance, _level(thread.classification)):
                raise PermissionError("reader clearance is insufficient for this thread")
            unread_count = await self._unread_count(connection, thread_id, reader_id)
        return thread.model_copy(update={"unread_count": unread_count})

    async def list_threads(
        self,
        inbox_id: str,
        *,
        reader_id: str,
        cursor: str | None = None,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> ThreadPage:
        _validate_limit(limit)
        clearance = _level(classification_max)
        async with self._pool.acquire() as connection:
            inbox = await self._get_inbox(connection, inbox_id)
            if inbox.owner.participant_id != reader_id:
                raise PermissionError("reader must own the inbox")
            allowed = _allowed_labels(clearance)
            parameters: list[Any] = [inbox_id, _participant_match(reader_id), allowed]
            statement = """
                SELECT payload FROM inbox_threads
                WHERE inbox_id=$1
                  AND payload->'participants' @> $2::jsonb
                  AND payload->>'classification' = ANY($3::text[])
            """
            if cursor is not None:
                timestamp, identifier = _decode_cursor(cursor, inbox_id)
                anchor = await self._get_thread(connection, identifier)
                if (
                    anchor.inbox_id != inbox_id
                    or anchor.updated_at != timestamp
                    or reader_id not in {item.participant_id for item in anchor.participants}
                    or not dominates(clearance, _level(anchor.classification))
                ):
                    raise ValueError("cursor no longer references an item in this page")
                parameters.extend([timestamp, identifier])
                statement += " AND (updated_at, thread_id) < ($4::timestamptz, $5)"
            parameters.append(limit + 1)
            statement += f" ORDER BY updated_at DESC, thread_id DESC LIMIT ${len(parameters)}"
            rows = await connection.fetch(statement, *parameters)
            threads = [_model_from(row, Thread) for row in rows[:limit]]
            item_list: list[Thread] = []
            for thread in threads:
                item_list.append(
                    thread.model_copy(
                        update={
                            "unread_count": await self._unread_count(
                                connection, thread.thread_id, reader_id
                            )
                        }
                    )
                )
            items = tuple(item_list)
        return ThreadPage(items=items, page_info=_page_info(items, len(rows) > limit, inbox_id))

    async def append_message(
        self,
        thread_id: str,
        *,
        sender: Participant,
        recipients: tuple[Participant, ...],
        body: str,
        attachments: tuple[str, ...] = (),
        reply_to_id: str | None = None,
        trace: TraceMetadata | None = None,
        message_id: str | None = None,
    ) -> Message:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                thread = await self._get_thread(connection, thread_id, for_update=True)
                participant_ids = {item.participant_id for item in thread.participants}
                if sender.participant_id not in participant_ids or any(
                    item.participant_id not in participant_ids for item in recipients
                ):
                    raise ValueError("sender and recipients must participate in the thread")
                if reply_to_id is not None:
                    parent = await self._get_message(connection, reply_to_id)
                    if parent.thread_id != thread_id:
                        raise ValueError("reply_to_id must reference a message in this thread")
                message = Message(
                    message_id=message_id or _id("message"),
                    thread_id=thread_id,
                    sender=sender,
                    recipients=recipients,
                    body=body,
                    attachments=attachments,
                    reply_to_id=reply_to_id,
                    trace=trace or TraceMetadata(classification=thread.classification),
                )
                if message.trace.classification != thread.classification:
                    raise ValueError("message classification must match thread classification")
                result = await connection.execute(
                    "INSERT INTO inbox_messages(message_id, thread_id, payload, created_at) "
                    "VALUES ($1, $2, $3::jsonb, $4) "
                    "ON CONFLICT(message_id) DO NOTHING",
                    message.message_id,
                    thread_id,
                    _model_json(message),
                    message.created_at,
                )
                if not str(result).endswith("1"):
                    return await self._get_message(connection, message.message_id)
                updated_thread = thread.model_copy(
                    update={
                        "updated_at": message.created_at,
                        "last_message_id": message.message_id,
                    }
                )
                await connection.execute(
                    "UPDATE inbox_threads SET payload=$1::jsonb, updated_at=$2 WHERE thread_id=$3",
                    _model_json(updated_thread),
                    updated_thread.updated_at,
                    thread_id,
                )
        return message

    async def record_event_with_outbox(
        self,
        *,
        event_id: str,
        sender: Participant,
        recipients: tuple[Participant, ...],
        body: str,
        attachments: tuple[str, ...] = (),
        external_thread_id: str | None = None,
        subject: str | None = None,
        reply_to_event_id: str | None = None,
        trace: TraceMetadata | None = None,
        envelope: dict[str, object],
    ) -> tuple[Message, ...]:
        """Persist all inbox copies and transport work in one DB transaction."""
        if not recipients:
            raise ValueError("at least one recipient is required")
        participants = tuple(dict.fromkeys((sender, *recipients)))
        classification = trace.classification if trace else "UNCLASSIFIED"

        def stable(prefix: str, *parts: str) -> str:
            return prefix + "_" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

        copies: list[Message] = []
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                for owner in participants:
                    inbox_id = stable("inbox", owner.participant_id)
                    inbox = Inbox(owner=owner, classification=classification, inbox_id=inbox_id)
                    await connection.execute(
                        "INSERT INTO inboxes(inbox_id, payload, created_at) "
                        "VALUES ($1,$2::jsonb,$3) "
                        "ON CONFLICT(inbox_id) DO NOTHING",
                        inbox_id,
                        _model_json(inbox),
                        inbox.created_at,
                    )
                    stored_inbox = await self._get_inbox(connection, inbox_id)
                    if not dominates(_level(stored_inbox.classification), _level(classification)):
                        raise ValueError("mail classification exceeds inbox classification")
                    thread_id = stable("thread", inbox_id, external_thread_id or event_id)
                    thread = Thread(
                        thread_id=thread_id,
                        inbox_id=inbox_id,
                        participants=participants,
                        subject=subject,
                        classification=classification,
                    )
                    await connection.execute(
                        "INSERT INTO inbox_threads(thread_id,inbox_id,payload,updated_at) "
                        "VALUES ($1,$2,$3::jsonb,$4) "
                        "ON CONFLICT(thread_id) DO NOTHING",
                        thread_id,
                        inbox_id,
                        _model_json(thread),
                        thread.updated_at,
                    )
                    stored_thread = await self._get_thread(connection, thread_id, for_update=True)
                    _require_thread_contract(stored_thread, participants, classification, subject)
                    message_id = stable("message", inbox_id, event_id)
                    reply_to_id = None
                    if reply_to_event_id:
                        reply_to_id = (
                            reply_to_event_id
                            if reply_to_event_id.startswith("message_")
                            else stable("message", inbox_id, reply_to_event_id)
                        )
                    message = Message(
                        message_id=message_id,
                        thread_id=stored_thread.thread_id,
                        sender=sender,
                        recipients=recipients,
                        body=body,
                        attachments=attachments,
                        reply_to_id=reply_to_id,
                        trace=trace or TraceMetadata(classification=stored_thread.classification),
                    )
                    result = await connection.execute(
                        "INSERT INTO inbox_messages(message_id,thread_id,payload,created_at) "
                        "VALUES ($1,$2,$3::jsonb,$4) "
                        "ON CONFLICT(message_id) DO NOTHING",
                        message_id,
                        thread_id,
                        _model_json(message),
                        message.created_at,
                    )
                    if str(result).endswith("1"):
                        updated = stored_thread.model_copy(
                            update={
                                "updated_at": message.created_at,
                                "last_message_id": message_id,
                            }
                        )
                        await connection.execute(
                            "UPDATE inbox_threads SET payload=$1::jsonb,updated_at=$2 "
                            "WHERE thread_id=$3",
                            _model_json(updated),
                            updated.updated_at,
                            thread_id,
                        )
                    copies.append(message)
                await connection.execute(
                    "INSERT INTO mail_outbox(event_id,envelope) VALUES ($1,$2::jsonb) "
                    "ON CONFLICT(event_id) DO NOTHING",
                    event_id,
                    _json(envelope),
                )
        return tuple(copies)

    async def list_messages(
        self,
        thread_id: str,
        *,
        reader_id: str,
        cursor: str | None = None,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> MessagePage:
        _validate_limit(limit)
        clearance = _level(classification_max)
        async with self._pool.acquire() as connection:
            thread = await self._get_thread(connection, thread_id)
            _require_participant(thread, reader_id)
            if not dominates(clearance, _level(thread.classification)):
                return MessagePage(items=(), page_info=PageInfo())
            parameters: list[Any] = [
                thread_id,
                thread.classification,
                _participant_match(reader_id),
                reader_id,
            ]
            statement = """
                SELECT payload FROM inbox_messages
                WHERE thread_id=$1 AND payload->'trace'->>'classification'=$2
                  AND (payload->'recipients' @> $3::jsonb
                       OR payload->'sender'->>'participant_id'=$4)
            """
            if cursor is not None:
                timestamp, identifier = _decode_cursor(cursor, thread_id)
                anchor = await self._get_message(connection, identifier)
                if (
                    anchor.thread_id != thread_id
                    or anchor.created_at != timestamp
                    or anchor.trace.classification != thread.classification
                    or not _message_visible_to(anchor, reader_id)
                ):
                    raise ValueError("cursor no longer references an item in this page")
                parameters.extend([timestamp, identifier])
                statement += " AND (created_at, message_id) > ($5::timestamptz, $6)"
            parameters.append(limit + 1)
            statement += f" ORDER BY created_at ASC, message_id ASC LIMIT ${len(parameters)}"
            rows = await connection.fetch(statement, *parameters)
        items = tuple(_model_from(row, Message) for row in rows[:limit])
        return MessagePage(items=items, page_info=_page_info(items, len(rows) > limit, thread_id))

    async def mark_read(self, message_id: str, reader_id: str) -> Message:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                message = await self._get_message(connection, message_id, for_update=True)
                if reader_id not in {item.participant_id for item in message.recipients}:
                    raise ValueError("reader must be a message recipient")
                receipts = [
                    receipt
                    for receipt in message.read_receipts
                    if receipt.participant_id != reader_id
                ]
                receipts.append(
                    ReadReceipt(participant_id=reader_id, state=ReadState.READ, read_at=_now())
                )
                updated = message.model_copy(update={"read_receipts": tuple(receipts)})
                await connection.execute(
                    "UPDATE inbox_messages SET payload=$1::jsonb WHERE message_id=$2",
                    _model_json(updated),
                    message_id,
                )
        return updated

    async def create_handoff(
        self,
        thread_id: str,
        *,
        from_participant: Participant,
        to_participants: tuple[Participant, ...],
        source_message_id: str | None,
        trace: TraceMetadata,
        handoff_id: str | None = None,
    ) -> Handoff:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                thread = await self._get_thread(connection, thread_id, for_update=True)
                participant_ids = {item.participant_id for item in thread.participants}
                if from_participant.participant_id not in participant_ids or any(
                    item.participant_id not in participant_ids for item in to_participants
                ):
                    raise ValueError("handoff participants must belong to the thread")
                if source_message_id is not None:
                    source = await self._get_message(connection, source_message_id)
                    if source.thread_id != thread_id:
                        raise ValueError(
                            "source_message_id must reference a message in this thread"
                        )
                if trace.classification != thread.classification:
                    raise ValueError("handoff classification must match thread classification")
                handoff = Handoff(
                    handoff_id=handoff_id or _id("handoff"),
                    thread_id=thread_id,
                    from_participant=from_participant,
                    to_participants=to_participants,
                    source_message_id=source_message_id,
                    trace=trace,
                )
                result = await connection.execute(
                    "INSERT INTO inbox_handoffs(handoff_id, thread_id, payload, created_at) "
                    "VALUES ($1, $2, $3::jsonb, $4) ON CONFLICT(handoff_id) DO NOTHING",
                    handoff.handoff_id,
                    thread_id,
                    _model_json(handoff),
                    handoff.created_at,
                )
                if not str(result).endswith("1"):
                    return await self._get_handoff(connection, handoff.handoff_id)
        return handoff

    async def resolve_handoff(
        self,
        handoff_id: str,
        *,
        recipient: Participant,
        actor_did: str,
        status: HandoffStatus,
    ) -> Handoff:
        if status is HandoffStatus.PENDING:
            raise ValueError("handoff must be accepted or declined")
        if not actor_did.startswith("did:"):
            raise ValueError("handoff resolution actor must be a DID")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                handoff = await self._get_handoff(connection, handoff_id, for_update=True)
                if recipient.participant_id not in {
                    item.participant_id for item in handoff.to_participants
                }:
                    raise PermissionError("only an addressed recipient can resolve a handoff")
                if handoff.status is not HandoffStatus.PENDING:
                    if (
                        handoff.status is status
                        and handoff.resolved_by == recipient
                        and handoff.resolved_actor_did == actor_did
                    ):
                        return handoff
                    raise ValueError("handoff is already resolved")
                updated = handoff.model_copy(
                    update={
                        "status": status,
                        "resolved_by": recipient,
                        "resolved_actor_did": actor_did,
                        "resolved_at": _now(),
                    }
                )
                await connection.execute(
                    "UPDATE inbox_handoffs SET payload=$1::jsonb WHERE handoff_id=$2",
                    _model_json(updated),
                    handoff_id,
                )
        return updated

    async def list_handoffs(
        self, thread_id: str, *, reader_id: str, classification_max: str = "UNCLASSIFIED"
    ) -> tuple[Handoff, ...]:
        clearance = _level(classification_max)
        async with self._pool.acquire() as connection:
            thread = await self._get_thread(connection, thread_id)
            _require_participant(thread, reader_id)
            rows = await connection.fetch(
                "SELECT payload FROM inbox_handoffs WHERE thread_id=$1 "
                "AND payload->'trace'->>'classification' = ANY($2::text[]) "
                "ORDER BY created_at ASC, handoff_id ASC",
                thread_id,
                _allowed_labels(clearance),
            )
        return tuple(_model_from(row, Handoff) for row in rows)

    @property
    def _pool(self) -> Any:
        return self._backend._require_pool()

    async def _get_inbox(self, connection: Any, inbox_id: str) -> Inbox:
        row = await connection.fetchrow("SELECT payload FROM inboxes WHERE inbox_id=$1", inbox_id)
        if row is None:
            raise KeyError(f"unknown inbox: {inbox_id}")
        return _model_from(row, Inbox)

    async def _get_thread(
        self, connection: Any, thread_id: str, *, for_update: bool = False
    ) -> Thread:
        statement = "SELECT payload FROM inbox_threads WHERE thread_id=$1"
        if for_update:
            statement += " FOR UPDATE"
        row = await connection.fetchrow(statement, thread_id)
        if row is None:
            raise KeyError(f"unknown thread: {thread_id}")
        return _model_from(row, Thread)

    async def _get_message(
        self, connection: Any, message_id: str, *, for_update: bool = False
    ) -> Message:
        statement = "SELECT payload FROM inbox_messages WHERE message_id=$1"
        if for_update:
            statement += " FOR UPDATE"
        row = await connection.fetchrow(statement, message_id)
        if row is None:
            raise KeyError(f"unknown message: {message_id}")
        return _model_from(row, Message)

    async def _get_handoff(
        self, connection: Any, handoff_id: str, *, for_update: bool = False
    ) -> Handoff:
        statement = "SELECT payload FROM inbox_handoffs WHERE handoff_id=$1"
        if for_update:
            statement += " FOR UPDATE"
        row = await connection.fetchrow(statement, handoff_id)
        if row is None:
            raise KeyError(f"unknown handoff: {handoff_id}")
        return _model_from(row, Handoff)

    async def _unread_count(self, connection: Any, thread_id: str, reader_id: str) -> int:
        rows = await connection.fetch(
            "SELECT payload FROM inbox_messages WHERE thread_id=$1 "
            "AND payload->'recipients' @> $2::jsonb",
            thread_id,
            _participant_match(reader_id),
        )
        return sum(
            message.state_for(reader_id) is ReadState.UNREAD
            for message in (_model_from(row, Message) for row in rows)
        )


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _level(value: str) -> Classification:
    return parse_classification(value, strict=True)


def _allowed_labels(clearance: Classification) -> list[str]:
    return [level.name for level in Classification if dominates(clearance, level)]


def _model_json(model: BaseModel) -> str:
    return _json(model.model_dump(mode="json"))


def _model_from(row: Any, model_type: type[_Model]) -> _Model:
    return model_type.model_validate(_as_dict(row["payload"]))


def _participant_match(participant_id: str) -> str:
    return json.dumps([{"participant_id": participant_id}], separators=(",", ":"))


def _require_participant(thread: Thread, reader_id: str) -> None:
    if reader_id not in {item.participant_id for item in thread.participants}:
        raise PermissionError("reader must participate in the thread")


def _require_thread_contract(
    thread: Thread,
    participants: tuple[Participant, ...],
    classification: str,
    subject: str | None,
) -> None:
    """Reject an idempotency-key collision that changes a mail thread's authority."""
    if {item.participant_id for item in thread.participants} != {
        item.participant_id for item in participants
    }:
        raise ValueError("existing mail thread has different participants")
    if thread.classification != classification:
        raise ValueError("existing mail thread has different classification")
    if thread.subject != subject:
        raise ValueError("existing mail thread has different subject")


def _message_visible_to(message: Message, reader_id: str) -> bool:
    return message.sender.participant_id == reader_id or reader_id in {
        item.participant_id for item in message.recipients
    }


def _validate_limit(limit: int) -> None:
    if limit < 1 or limit > _MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")


def _encode_cursor(scope: str, item: Thread | Message) -> str:
    timestamp = item.updated_at if isinstance(item, Thread) else item.created_at
    identifier = item.thread_id if isinstance(item, Thread) else item.message_id
    payload = json.dumps({"scope": scope, "timestamp": timestamp.isoformat(), "id": identifier})
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, scope: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload["scope"] != scope:
            raise ValueError("cursor scope does not match query")
        timestamp = datetime.fromisoformat(payload["timestamp"])
        if (
            timestamp.tzinfo is None
            or timestamp.utcoffset() is None
            or not isinstance(payload["id"], str)
        ):
            raise ValueError("cursor must include a timezone and identifier")
        return timestamp, payload["id"]
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("invalid cursor") from exc


def _page_info(
    items: tuple[Thread, ...] | tuple[Message, ...], has_more: bool, scope: str
) -> PageInfo:
    if not items or not has_more:
        return PageInfo()
    return PageInfo(next_cursor=_encode_cursor(scope, items[-1]), has_more=True)
