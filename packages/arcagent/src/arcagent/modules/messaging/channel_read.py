"""Read a team channel's recent history for an agent (Alpha 2 item 58).

Conversations live in group channels, so an agent that was not awake for them
must be able to catch up. The read is deliberately narrow: membership-gated,
no-read-up against both the channel and each message, free of other agents'
un-addressed chatter (ADR-032), bounded in messages and bytes, and audited.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from arctrust.classification import Classification

from arcagent.modules.messaging import activation, mail_turn
from arcagent.utils.sanitizer import sanitize_text

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_PAGE_BYTES = 16_000
MAX_BODY_CHARS = 4_000
_SCAN_PAGE = 500
NOT_A_MEMBER = {"error": "not a member"}


def clamp_limit(limit: int) -> int:
    """Bound the requested page size to ``1..MAX_LIMIT``."""
    return max(1, min(int(limit), MAX_LIMIT))


def _level(label: str) -> Classification | None:
    """Ladder rung for a label; ``None`` for an unknown one (callers fail closed)."""
    return Classification.__members__.get(str(label).upper())


def _dominated(clearance: Classification, label: str) -> bool:
    level = _level(label)
    return level is not None and clearance >= level


def _is_member(st: Any, members: list[str]) -> bool:
    mine = {st.config.entity_id}
    if st.identity is not None:
        mine.add(st.identity.did)
    return bool(mine & set(members))


async def _find_channel(st: Any, name: str) -> Any | None:
    for channel in await st.svc.list_channels():
        if channel.name == name:
            return channel
    return None


async def _tail(st: Any, channel: str, limit: int, before_seq: int) -> list[Any]:
    """The newest ``limit`` messages with ``seq < before_seq``, newest first.

    Built on ``list_channel_messages`` (oldest-first, cursor-free): pages are
    streamed through a bounded deque, so memory stays at ``limit`` messages
    however long the channel is.
    """
    window: deque[Any] = deque(maxlen=limit)
    after = 0
    while True:
        page = await st.svc.list_channel_messages(channel, after_seq=after, limit=_SCAN_PAGE)
        if not page:
            break
        window.extend(m for m in page if before_seq <= 0 or m.seq < before_seq)
        after = page[-1].seq
        if len(page) < _SCAN_PAGE:
            break
    return list(reversed(window))


def _audit(st: Any, channel: str, outcome: str, **detail: Any) -> None:
    if st.telemetry is None:
        return
    st.telemetry.audit_event(
        "messaging.channel_read",
        {
            "channel": channel,
            "reader": st.identity.did if st.identity is not None else st.config.entity_id,
            "outcome": outcome,
            **detail,
        },
    )


def _render(message: Any) -> dict[str, Any]:
    return {
        "seq": message.seq,
        "id": message.id,
        "sender": sanitize_text(str(message.sender), max_length=200),
        "body": sanitize_text(str(message.body), max_length=MAX_BODY_CHARS),
        "ts": message.ts,
        "thread_id": message.thread_id,
    }


async def read_channel(st: Any, channel: str, limit: int, before_seq: int) -> dict[str, Any]:
    """Return one newest-first page of *channel*, or the denial."""
    record = await _find_channel(st, channel)
    clearance = Classification[mail_turn.clearance(st)]
    if (
        record is None
        or not _is_member(st, list(record.members))
        or not _dominated(clearance, record.clearance)
    ):
        _audit(st, channel, "denied")
        return dict(NOT_A_MEMBER)

    page = await _tail(st, channel, clamp_limit(limit), before_seq)
    peers = await activation.other_agent_dids(st)
    shown: list[dict[str, Any]] = []
    omitted = 0
    used = 0
    truncated = False
    cursor: int | None = None
    for message in page:
        if activation.hidden_from_context(message, st.identity, peers) or not _dominated(
            clearance, message.classification
        ):
            omitted += 1
            cursor = message.seq
            continue
        item = _render(message)
        used += len(item["body"].encode())
        if used > MAX_PAGE_BYTES:
            truncated = True
            break
        shown.append(item)
        cursor = message.seq
    # A cursor is offered whenever older messages may remain: the page was cut by
    # the byte cap, or it came back full.
    more = truncated or len(page) >= clamp_limit(limit)
    result = {
        "channel": channel,
        "messages": shown,
        "omitted": omitted,
        "truncated": truncated,
        "next_before_seq": cursor if more else None,
    }
    _audit(st, channel, "ok", returned=len(shown), omitted=omitted, truncated=truncated)
    return result
