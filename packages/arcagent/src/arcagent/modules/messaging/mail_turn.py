"""The mail turn: inbound mail wakes one turn, and its answer is the thread's one reply.

Mail is a task, a question or an info share. It carries one message and at most
one reply (ArcTeam's ``AgentMailService`` enforces it). Conversation belongs in
the team channel, where the operator and every agent can see it, so the wake
prompt steers there.

A mail turn runs with reply target ``mail://<conversation id>``. The turn's
final text is posted into that conversation once, under an idempotency key bound
to the run id, so a redelivered or retried finalizer can never post twice and a
silent (FYI) turn posts nothing.

ArcTeam is an optional dependency of this module, so it is imported lazily.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from arcagent.core.turn_context import MAIL_TARGET_SCHEME, mail_conversation
from arcagent.modules.messaging import _runtime
from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.messaging.mail_turn")


@dataclass(frozen=True)
class InboundMail:
    """The mail driving the current turn, for tools that act on it (``mail_handoff``)."""

    conversation_id: str
    sender: str
    subject: str | None
    body: str


_inbound: contextvars.ContextVar[InboundMail | None] = contextvars.ContextVar(
    "arcagent_inbound_mail", default=None
)


def bind_inbound(mail: InboundMail | None) -> contextvars.Token[InboundMail | None]:
    """Bind the mail driving the turn about to start; the run copies this context."""
    return _inbound.set(mail)


def unbind_inbound(token: contextvars.Token[InboundMail | None]) -> None:
    """Restore the previous binding once the turn has been handed off."""
    _inbound.reset(token)


def inbound() -> InboundMail | None:
    """The mail driving this turn, or ``None`` outside a mail turn."""
    return _inbound.get()


def is_mail(message: Any) -> bool:
    """Whether ``message`` is an explicit mail envelope (not channel chat)."""
    return str(getattr(message, "delivery_kind", "chat")) == "mail"


def conversation_id(message: Any) -> str:
    """The transport conversation a mail envelope belongs to."""
    return str(message.thread_id or message.id)


def reply_target(message: Any) -> tuple[str, str]:
    """The ``mail://`` reply target and display label for a mail envelope."""
    subject = sanitize_text(str(message.subject or "no subject"), max_length=200)
    return f"{MAIL_TARGET_SCHEME}{conversation_id(message)}", f"Mail — {subject}"


def inbound_record(message: Any) -> InboundMail:
    """The mail fields a tool inside the turn may act on."""
    return InboundMail(
        conversation_id=conversation_id(message),
        sender=str(message.signer_did or message.sender),
        subject=message.subject,
        body=str(message.body),
    )


def idempotency_key(run_id: str | None, final_text: str) -> str:
    """Bind a reply to its run, so a retried finalizer replays rather than duplicates."""
    if run_id:
        return f"run:{run_id}"
    return "text:" + hashlib.sha256(final_text.encode("utf-8")).hexdigest()[:32]


def clearance(st: Any) -> str:
    """The agent's clearance: the ceiling for what it may read or write in mail."""
    return str(st.identity.clearance.name) if st.identity is not None else "UNCLASSIFIED"


async def handle_of(st: Any, did: str) -> str:
    """Render a sender DID as ``@handle``; an unknown sender stays a sanitised id."""
    handle = ""
    try:
        entity: Any = await st.registry.get(did)
    except Exception:  # reason: a roster outage must not block the wake
        entity = None
    if entity is not None:
        handle = str(entity.handle or "")
    if handle:
        return "@" + sanitize_text(handle, max_length=100)
    return sanitize_text(did, max_length=200)


def _addressed_only_by_copy(st: Any, message: Any) -> bool:
    me = {st.config.entity_id}
    if st.identity is not None:
        me.add(st.identity.did)
    to = {str(item) for item in (message.to or [])}
    cc = {str(item) for item in (getattr(message, "cc", None) or [])}
    return not (me & to) and bool(me & cc)


async def format_delivery(st: Any, message: Any) -> str:
    """Render one inbound mail as a sanitised wake prompt (LLM01)."""
    sender = await handle_of(st, str(message.signer_did or message.sender))
    subject = sanitize_text(str(message.subject or "no subject"), max_length=200)
    thread = sanitize_text(conversation_id(message), max_length=200)
    body = sanitize_text(str(message.body), max_length=4000)
    lines = [
        f"Mail from {sender} — subject: {subject} — thread {thread}",
        f"> {body}",
    ]
    if _addressed_only_by_copy(st, message):
        lines.append("You were copied on this mail. No reply is expected: finish without text.")
    else:
        lines.append(
            "Reply only when this mail needs an answer: a question, a result the sender "
            "asked for, or information the sender must pass to the operator. Your final "
            "answer is posted to this thread automatically, once. For an FYI or an "
            "acknowledgement, finish without text."
        )
    lines.append(
        "Mail allows one reply only. For discussion or follow-ups, post in the team "
        "channel (messaging_send to channel://<name>); read a channel's recent history "
        "with messaging_read_channel. For directed work, use "
        "create_task / assign_task. To pass this mail to a better-placed teammate, "
        "use mail_handoff."
    )
    return "\n".join(lines)


async def admit(st: Any, message: Any) -> str | None:
    """Refuse mail that may not wake the agent; return the refusal reason or ``None``.

    Raises ``RetryableDeliveryError`` when the durable store cannot be reached, so
    the bus redelivers the mail instead of dropping it.
    """
    from arcteam import MailThreadClosedError, RetryableDeliveryError

    try:
        mail = await _runtime.ensure_agent_mail()
    except RuntimeError:
        return "mail_store_unavailable"
    try:
        await mail.check_inbound(message, classification_max=clearance(st))
    except MailThreadClosedError:
        return "mail_thread_closed"
    except KeyError:
        return "mail_not_durable"
    except (OSError, ConnectionError, TimeoutError) as exc:
        raise RetryableDeliveryError(str(message.id)) from exc
    return None


async def deliver_reply(st: Any, target: str, final_text: str, run_id: str | None) -> None:
    """Post a mail turn's final text as the conversation's one reply. Never raises."""
    from arcteam import MailThreadClosedError

    conversation = mail_conversation(target)
    if conversation is None:
        return
    outcome = "sent"
    try:
        mail = await _runtime.ensure_agent_mail()
        await mail.reply_to_conversation(
            conversation,
            body=final_text,
            idempotency_key=idempotency_key(run_id, final_text),
            classification_max=clearance(st),
        )
    except MailThreadClosedError:
        outcome = "thread_closed"
    except Exception as exc:  # reason: a failed reply must not crash the finalizer
        outcome = "failed"
        _logger.warning("mail reply into %s failed: %s", conversation, type(exc).__name__)
    if st.telemetry is not None:
        st.telemetry.audit_event(
            "messaging.mail_reply",
            {"conversation_id": conversation, "run_id": run_id or "", "outcome": outcome},
        )


__all__ = [
    "InboundMail",
    "admit",
    "bind_inbound",
    "clearance",
    "conversation_id",
    "deliver_reply",
    "format_delivery",
    "handle_of",
    "idempotency_key",
    "inbound",
    "inbound_record",
    "is_mail",
    "reply_target",
    "unbind_inbound",
]
