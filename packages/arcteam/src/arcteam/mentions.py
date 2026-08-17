"""Mention extraction, routing scope, and attention flags (REQ-004).

``extract_mentions`` is a pure regex extractor; ``apply_mentions`` resolves
those handles to DIDs on a message and raises its attention flags;
``unresolved_mentions`` reports the handles that named nobody.

A resolved mention **routes**. It scopes which channel members wake at all
(SPEC-055 ``_should_activate``) and is fanned into each mentioned entity's inbox
so the addressee wakes regardless of channel membership
(``MessagingService._fanout_mentions_to_inboxes``). Only an *unresolvable*
handle is treated as prose.
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
    ignored here — a surface with a human on the other end should call
    :func:`unresolved_mentions` first and refuse, because silently dropping the
    handle turns an addressed message into an un-addressed broadcast. When at
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


def unresolved_mentions(entities: list[Entity], body: str) -> list[str]:
    """Return the ordered, de-duplicated ``@handle`` tokens naming no entity.

    The query a surface uses to refuse a post before sending it. ``@sales_agent``
    against a registry holding ``sales`` resolves to nothing, and
    :func:`apply_mentions` then produces a message with no mentions at all — no
    inbox fanout, no ``action_required``, no priority bump — which is
    indistinguishable from having typed no address. Naming the bad handle back
    to whoever typed it is the only way that is recoverable.
    """
    return [handle for handle in extract_mentions(body) if not _resolves(entities, handle)]


def _resolves(entities: list[Entity], handle: str) -> bool:
    """Whether ``@handle`` names a registered entity in ``entities``."""
    try:
        resolve_ref(entities, f"@{handle}")
    except UnknownHandle:
        return False
    return True
