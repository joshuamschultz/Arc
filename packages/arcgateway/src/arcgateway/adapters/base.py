"""The whole contract a platform adapter implements (SPEC-065 COMP-004).

REQ-310 states the surface as a *negative*: an adapter holds its connection,
turns a platform payload into parts, and sends parts back — and nothing else.
Download, naming, size ceilings, audit, session identity, pairing and message
splitting are the gateway's, written once. Four adapters each enforcing their
own size cap is four chances to forget it.

The seam that makes this possible is :class:`PendingMedia`. An adapter knows
how to *fetch* an artefact off its own wire and nothing else about it: it does
not name the file, choose where it lands, or decide whether it is small enough.
It hands the gateway a callable, and the gateway takes custody
(:class:`~arcgateway.media_custody.MediaCustodian` →
:class:`~arcgateway.media_store.MediaStore`).

Reconnect-watcher and FailedAdapter state live in ``_reconnect.py``.

Adapter lifecycle states:
    CONNECTING → CONNECTED → DISCONNECTING → DISCONNECTED
                          ↘ FAILED → (reconnect watcher retries)
                                   → PERMANENTLY_FAILED (after 20 attempts)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import Part, TextPart

#: Artefact classes the part vocabulary carries (mirrors ``MediaPart.kind``).
MediaKind = Literal["image", "file", "audio"]

class MediaTooLargeOnWireError(Exception):
    """A fetch was abandoned because the artefact passed the ceiling mid-read.

    Distinct from :class:`~arcgateway.media_store.MediaTooLargeError`, which
    refuses bytes it has measured. This one is raised by the bytes that were
    never paid for: the adapter stops reading the moment the running total
    passes the bound, so the true size is unknown and deliberately unreported.
    """

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"artefact exceeded the {limit_bytes} byte ceiling mid-download")
        self.limit_bytes = limit_bytes


@dataclass(frozen=True)
class PendingMedia:
    """An artefact the platform holds and the gateway has not yet taken custody of.

    Attributes:
        kind: Artefact class the agent will dispatch on.
        mime: Media type as the platform declared it.
        declared_name: Sender-supplied filename. Untrusted — metadata only.
        size_bytes: Size the platform declared, when it declares one. Untrusted,
            so it may only be used to refuse *early* — never to accept, which
            stays with the bytes that actually arrive.
        fetch: Awaited by the gateway, once, with the ceiling in bytes. The
            adapter must not read the bytes into the envelope itself; a photo
            that rides in the envelope ends up in the session log and the
            prompt, which is exactly what the reference model prevents. The
            ceiling is passed rather than owned so the *decision* stays the
            gateway's (REQ-310) while the bounded read stays where the wire is:
            an adapter that only refuses after reading has already spent the
            memory an attacker asked it to spend.

    Raises:
        MediaTooLargeOnWireError: From ``fetch``, when the artefact passes the
            ceiling. Nothing beyond the bound is read.
    """

    kind: MediaKind
    mime: str
    declared_name: str
    fetch: Callable[[int], Awaitable[bytes]]
    size_bytes: int | None = None


#: What an adapter may put in a draft: words, or an artefact it can go and get.
DraftPart = TextPart | PendingMedia


@dataclass(frozen=True)
class InboundDraft:
    """What an adapter hands up: platform identity plus ordered parts.

    Deliberately *not* an :class:`~arcgateway.executor.InboundEvent`. A draft
    still names artefacts the gateway has not fetched, and it carries no
    ``session_key`` — session identity belongs to ``SessionRouter`` alone
    (REQ-304, REQ-310), and an adapter that composed one would be a second
    identity for the same (agent, user) pair.

    Attributes:
        platform: Source platform name ("telegram", "slack", …).
        chat_id: Platform-specific conversation identifier.
        user_did: Platform-scoped sender identity; the router resolves it.
        agent_did: DID of the agent this adapter's bot serves.
        parts: Ordered parts, exactly as the sender composed them.
        thread_id: Optional thread within the chat.
        raw_payload: Full platform payload for audit/replay.
    """

    platform: str
    chat_id: str
    user_did: str
    agent_did: str
    parts: Sequence[DraftPart] = ()
    thread_id: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


#: What ``send`` accepts. A bare string is the one-text-part shorthand the
#: streaming path uses token by token; ``[TextPart(...), MediaPart(...)]`` is
#: the full reply an agent composes (REQ-311).
Outbound = str | Sequence[Part]


def as_parts(outbound: Outbound) -> list[Part]:
    """Normalise either accepted shape to the one part vocabulary."""
    if isinstance(outbound, str):
        return [TextPart(text=outbound)]
    return list(outbound)


@runtime_checkable
class BasePlatformAdapter(Protocol):
    """Protocol that all platform adapters must satisfy.

    Three responsibilities, and no fourth:

    1. **Lifecycle** — hold the platform connection (``connect``/``disconnect``).
    2. **Translation** — turn one platform payload into parts (``to_parts``).
    3. **Delivery** — put parts back on the platform (``send``).

    The event-source loop (polling/websocket) runs as an asyncio.Task owned by
    the adapter and supervised by GatewayRunner's TaskGroup, so a crash in one
    adapter never kills its siblings (ASI08).
    """

    name: str
    """Platform identifier (e.g. "telegram", "slack"). Shared across bots of the
    same platform — combine with ``agent_did`` for a unique outbound key."""

    agent_did: str
    """DID of the agent this adapter's bot serves. The SessionRouter keys its
    outbound registry by (name, agent_did) so a reply returns through the RIGHT
    bot when several bots run on one platform (one per agent). "" for a single
    adapter that fronts every agent (e.g. web)."""

    async def connect(self) -> None:
        """Establish the platform connection.

        Called by GatewayRunner on startup and after each successful reconnect.
        Must return promptly (start background tasks, don't block).

        Raises:
            RuntimeError: If connection fails fatally (e.g. invalid credentials).
        """
        ...

    async def disconnect(self) -> None:
        """Gracefully shut down the platform connection.

        Must cancel any background tasks this adapter owns and should NOT raise.
        """
        ...

    def to_parts(self, payload: Any) -> list[DraftPart]:
        """Turn one platform payload into ordered parts.

        Text is a part like any other, so a caption between two photos is
        representable and media is never a branch. Artefacts come back as
        :class:`PendingMedia` — named and fetchable, not downloaded.

        Args:
            payload: The platform's own message object.

        Returns:
            Ordered parts as the sender composed them; empty when the payload
            carries nothing the gateway can act on.
        """
        ...

    async def send(
        self,
        target: DeliveryTarget,
        parts: Outbound,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Deliver a reply to ``target`` on this platform (REQ-311).

        Where the platform cannot carry a part's kind or size, the adapter
        degrades to a text description that names the file. Losing the turn is
        not an option a delivery guarantee permits: an operator who asked for a
        report and got silence has been failed, exception or no exception.

        Args:
            target: Parsed delivery address (platform, chat_id, thread_id).
            parts: The reply, as parts or as the plain-text shorthand.
            reply_to: Optional message ID to reply to (platform-specific).

        Raises:
            RuntimeError: On unrecoverable delivery failure. Transient failures
                (rate limits, network errors) are retried internally first.
        """
        ...

    async def send_with_id(
        self,
        target: DeliveryTarget,
        message: str,
    ) -> str | None:
        """Send a message and return its platform-assigned message ID.

        Default implementation calls send() and returns None. Adapters whose
        platform returns message IDs override this so StreamBridge can edit the
        message in place while a turn streams.
        """
        await self.send(target, message)
        return None
