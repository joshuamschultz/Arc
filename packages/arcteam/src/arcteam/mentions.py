"""Mention extraction and attention flags (REQ-004).

``extract_mentions`` is a pure regex extractor; ``apply_mentions`` resolves
those handles to DIDs on a message and raises its attention flags.
"""

from __future__ import annotations

import re

from arcteam.registry import UnknownHandle, resolve_ref
from arcteam.types import Entity, Message, Priority

_MENTION_RE = re.compile(r"@([a-z0-9_-]+)")


def extract_mentions(body: str) -> list[str]:
    """Return the ordered, de-duplicated ``@handle`` tokens in ``body``.

    Pure and synchronous: matches ``@[a-z0-9_-]+`` and strips the ``@``.
    """
    seen: dict[str, None] = {}
    for handle in _MENTION_RE.findall(body):
        seen.setdefault(handle, None)
    return list(seen)


def apply_mentions(entities: list[Entity], message: Message) -> None:
    """Resolve body mentions to DIDs and raise attention flags on ``message``.

    Resolves against a pre-fetched entity snapshot (the caller's single
    per-send registry read) rather than re-querying per mention. A body
    ``@handle`` that names no registered entity is treated as plain text and
    ignored — mentions are best-effort attention hints, not routing. When at
    least one mention resolves, ``action_required`` is set and the priority is
    raised to at least ``HIGH`` without downgrading a higher one.
    """
    dids: list[str] = []
    for handle in extract_mentions(message.body):
        try:
            dids.append(resolve_ref(entities, f"@{handle}"))
        except UnknownHandle:
            continue
    message.mentions = dids
    if not dids:
        return
    message.action_required = True
    if message.priority in (Priority.LOW, Priority.NORMAL):
        message.priority = Priority.HIGH
