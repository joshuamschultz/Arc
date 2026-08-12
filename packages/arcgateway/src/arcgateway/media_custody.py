"""Taking custody of inbound artefacts (SPEC-065 COMP-002, REQ-297/298/299).

Split out of ``SessionRouter`` because it is a whole responsibility with its
own failure vocabulary — fetch, ceiling, storage, and the answer the sender
gets for each — and the router's job is session identity and dispatch.

The rule every branch here obeys: **a message is never quietly diminished.**
An artefact that cannot be stored still travels as words naming it, and the
sender is always told what happened to what they attached. A photo that
vanishes with no reply is indistinguishable, from the sender's side, from an
agent that ignored them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from arcgateway.adapters.base import MediaTooLargeOnWireError, PendingMedia
from arcgateway.media_store import MediaStore, MediaTooLargeError
from arcgateway.parts import MediaPart, Part, TextPart, flatten_text

if TYPE_CHECKING:
    from arcgateway.adapters.base import InboundDraft
    from arcgateway.executor import InboundEvent

_logger = logging.getLogger("arcgateway.media_custody")


class MediaCustodian:
    """Turns an adapter's unfetched artefacts into workspace references.

    Args:
        media_store_for: Resolves the addressed agent's own store, or None when
            the agent has no workspace this gateway can see.
        send_reply: Answers the sender on the channel the message arrived on.
    """

    def __init__(
        self,
        *,
        media_store_for: Callable[[str], MediaStore | None] | None,
        send_reply: Callable[[InboundEvent, str], Awaitable[None]],
    ) -> None:
        self._media_store_for = media_store_for
        self._send_reply = send_reply

    async def take(self, draft: InboundDraft, event: InboundEvent) -> InboundEvent | None:
        """Fetch the draft's artefacts, store them, and reference them (REQ-297).

        The adapter said what the artefacts are and how to get them; every
        decision *about* them is made here — where they land, whether they are
        small enough, and the audit event that records the arrival. That is what
        makes a fourth platform unable to get any of it wrong.

        Args:
            draft: The adapter's draft, still naming unfetched artefacts.
            event: The canonicalised event, carrying the resolved identity the
                stored filename and audit event are attributed to.

        Returns:
            The event with each artefact replaced by a workspace reference or,
            where it could not be stored, by a line naming it. ``None`` only
            when the draft carried nothing at all.
        """
        channel = f"{draft.platform}:{draft.chat_id}"
        parts: list[Part] = []
        for part in draft.parts:
            if not isinstance(part, PendingMedia):
                parts.append(part)
                continue
            parts.append(await self._one(part, event, channel))

        if not parts:
            return None
        return event.model_copy(update={"parts": parts, "message": flatten_text(parts)})

    async def _one(self, pending: PendingMedia, event: InboundEvent, channel: str) -> Part:
        """Take custody of one artefact, or say in words why it is only named.

        Every outcome is answered on the channel the artefact arrived on
        (REQ-299) and every outcome yields a part, so the turn still carries
        what the sender sent even when the bytes could not be kept.
        """
        store = self._media_store_for(event.agent_did) if self._media_store_for else None
        if store is None:
            _logger.warning(
                "Media on %s for %s has no resolvable workspace — naming %r instead "
                "of storing it",
                channel,
                event.agent_did,
                pending.declared_name,
            )
            await self._send_reply(
                event,
                f"{pending.declared_name} arrived but could not be saved: this agent "
                f"has no workspace on this gateway. It is named in the message only.",
            )
            return self._named(pending, "no workspace")

        # The platform's declared size is untrusted, so it may refuse early but
        # never accept: the bytes that arrive are still measured by the store.
        if pending.size_bytes is not None and pending.size_bytes > store.max_bytes:
            await self._send_reply(
                event,
                f"{pending.declared_name} is too large to accept: "
                f"{pending.size_bytes} bytes is over the {store.max_bytes} byte limit.",
            )
            return self._named(pending, "too large")

        try:
            data = await pending.fetch(store.max_bytes)
        except MediaTooLargeOnWireError as exc:
            await self._send_reply(
                event,
                f"{pending.declared_name} is too large to accept: it passed the "
                f"{exc.limit_bytes} byte limit while downloading.",
            )
            return self._named(pending, "too large")
        except Exception:  # reason: fail-open — one artefact, not the turn
            _logger.exception("Could not fetch %r from %s", pending.declared_name, channel)
            await self._send_reply(
                event, f"{pending.declared_name} could not be downloaded from {channel}."
            )
            return self._named(pending, "download failed")

        try:
            stored = store.store(
                data=data,
                declared_name=pending.declared_name,
                mime=pending.mime,
                kind=pending.kind,
                sender=event.user_did,
                channel=channel,
                actor_did=event.user_did,
            )
        except MediaTooLargeError as exc:
            await self._send_reply(
                event,
                f"{pending.declared_name} is too large to accept: "
                f"{exc.size_bytes} bytes is over the {exc.limit_bytes} byte limit.",
            )
            return self._named(pending, "too large")

        return MediaPart(
            kind=pending.kind,
            mime=stored.mime,
            declared_name=stored.declared_name,
            ref=stored.ref,
        )

    @staticmethod
    def _named(pending: PendingMedia, reason: str) -> TextPart:
        """An artefact that could not be kept, as the words the agent still sees."""
        return TextPart(
            text=f"[{pending.kind} not stored: {pending.declared_name} ({reason})]"
        )


__all__ = ["MediaCustodian"]
