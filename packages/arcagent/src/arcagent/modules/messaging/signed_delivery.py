"""Signed teammate delivery through the accepted-run and reply owners."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from arcagent.core.run_contract import (
    ChannelReply,
    DeliveryUnavailableError,
    RunAdmissionUnavailableError,
    RunOutcomeUnknownError,
)


def run_id_for(message: Any, *, agent_did: str, session_key: str) -> str:
    """Bind redelivery to one agent, signed message ID, and session."""
    body = json.dumps(
        ["arc.team.message.run.v1", agent_did, session_key, message.id],
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(body).hexdigest()


async def deliver(
    st: Any,
    message: Any,
    *,
    prompt: str,
    session_key: str,
    reply_target: str | None,
    reply_label: str | None,
) -> str:
    """Await a signed accepted result and anchored reply before inbox ack."""
    issuer = st.trigger_issuer
    prepare = st.prepare_collected_request
    run_fn = st.agent_run_fn
    if issuer is None or prepare is None or run_fn is None or st.identity is None:
        raise DeliveryUnavailableError("signed inbox run capability is unavailable")
    if not message.id or not message.sig or not message.signer_did:
        raise RuntimeError("signed inbox envelope is incomplete")
    run_id = run_id_for(message, agent_did=st.identity.did, session_key=session_key)
    request = prepare(
        prompt,
        session_key=session_key,
        run_id=run_id,
        occurrence_id=message.id,
        run_purpose="message",
        caller_did=message.signer_did,
        reply_target=reply_target,
        reply_label=reply_label,
    )
    try:
        authorization, deadline = await issuer(request, message.model_dump_json().encode())
        result = await run_fn(
            prompt,
            session_key=session_key,
            run_id=run_id,
            occurrence_id=message.id,
            run_purpose="message",
            caller_did=message.signer_did,
            reply_target=reply_target,
            reply_label=reply_label,
            signed_authorization=authorization,
            authorization_deadline=deadline,
        )
    except RunOutcomeUnknownError:
        return "outcome_unknown"
    except (RunAdmissionUnavailableError, TimeoutError, OSError) as exc:
        raise DeliveryUnavailableError(message.id) from exc
    if result.outcome_unknown is not None:
        return "outcome_unknown"
    if reply_target is None:
        return "completed"
    reply_fn = st.accepted_reply_fn
    reply_port = st.reply_port
    if reply_fn is None or reply_port is None:
        raise DeliveryUnavailableError("accepted reply capability is unavailable")

    async def send(reply: ChannelReply) -> None:
        await reply_port.send_channel_reply(
            message_id=reply.message_id,
            sender=st.config.entity_id,
            target=reply.target,
            body=reply.text,
            classification=st.identity.clearance.name,
            hop=int(message.hop or 0) + 1,
        )

    async def lookup(reply: ChannelReply) -> bool:
        return cast(
            bool,
            await reply_port.find_sent(
                message_id=reply.message_id,
                target=reply.target,
                body_digest=reply.digest,
            ),
        )

    try:
        return cast(str, await reply_fn(run_id, send=send, lookup=lookup))
    except (RunAdmissionUnavailableError, TimeoutError, OSError) as exc:
        raise DeliveryUnavailableError(message.id) from exc
