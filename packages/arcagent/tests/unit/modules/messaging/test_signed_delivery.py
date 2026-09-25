"""Signed inbox delivery must bind the exact message and reuse one run."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import arcrun
import pytest
from arcteam import Channel, Entity, EntityType, Message, MessagingService
from arcteam.crypto import MessageSigner, sign_message, verify_message
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)
from packages.arcagent.tests.unit.modules.run_intents.test_reply_outbox import _ledger

from arcagent.core.run_contract import (
    CanonicalRunRequest,
    DeliveryUnavailableError,
    RunAdmissionUnavailableError,
)
from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import _wake_on
from arcagent.modules.run_intents.owner import LedgerRunOwner


@pytest.fixture(autouse=True)
def _reset_messaging_state() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _message(peer: AgentIdentity) -> Message:
    message = Message(
        id="signed-request-1",
        ts=datetime.now(UTC).isoformat(),
        sender="agent://peer",
        signer_did=peer.did,
        nonce="once-only",
        to=["channel://team"],
        body="check this",
    )
    sign_message(message, peer.signing_seed)
    return message


@pytest.mark.asyncio
async def test_signed_channel_redelivery_reuses_exact_run_and_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = AgentIdentity.generate(org="local", agent_type="agent")
    agent = AgentIdentity.generate(org="local", agent_type="agent")
    message = _message(peer)
    seen: set[str] = set()
    effects: list[str] = []
    sent: list[Any] = []
    service = SimpleNamespace(
        send_channel_reply=AsyncMock(
            side_effect=lambda **kwargs: sent.append(SimpleNamespace(id=kwargs["message_id"]))
        ),
        find_sent=AsyncMock(
            side_effect=lambda **kwargs: any(m.id == kwargs["message_id"] for m in sent)
        ),
    )

    def prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
        kwargs["purpose"] = kwargs.pop("run_purpose")
        return CanonicalRunRequest(input_text=prompt, **kwargs)

    async def issuer(request: CanonicalRunRequest, envelope: bytes) -> tuple[bytes, datetime]:
        asserted = Message.model_validate_json(envelope)
        assert verify_message(asserted, peer.public_key)
        assert asserted.id == request.occurrence_id
        assert asserted.signer_did == request.caller_did
        assert asserted.body in request.input_text
        assert request.reply_target == "channel://team"
        return request.digest().encode(), datetime.now(UTC) + timedelta(minutes=1)

    async def run_fn(prompt: str, **kwargs: Any) -> Any:
        assert (
            kwargs["signed_authorization"]
            == prepare(
                prompt,
                session_key=kwargs["session_key"],
                run_id=kwargs["run_id"],
                occurrence_id=kwargs["occurrence_id"],
                run_purpose=kwargs["run_purpose"],
                caller_did=kwargs["caller_did"],
                reply_target=kwargs["reply_target"],
                reply_label=kwargs["reply_label"],
            )
            .digest()
            .encode()
        )
        if kwargs["run_id"] not in seen:
            seen.add(kwargs["run_id"])
            effects.append(prompt)
        return SimpleNamespace(outcome_unknown=None)

    async def reply_fn(run_id: str, *, send: Any, lookup: Any) -> str:
        from arcagent.core.run_contract import ChannelReply

        reply = ChannelReply(
            message_id="reply_" + hashlib.sha256(run_id.encode()).hexdigest(),
            target="channel://team",
            text="done",
            digest=hashlib.sha256(b"done").hexdigest(),
        )
        if not await lookup(reply):
            await send(reply)
        return "sent"

    state = SimpleNamespace(
        identity=agent,
        config=SimpleNamespace(entity_id="agent://me"),
        processing_lock=asyncio.Lock(),
        channel_last_woken={},
        requires_signed_runs=True,
        trigger_issuer=issuer,
        prepare_collected_request=prepare,
        agent_run_fn=run_fn,
        accepted_reply_fn=reply_fn,
        reply_port=service,
        telemetry=None,
    )
    monkeypatch.setattr(_runtime, "state", lambda: state)
    await _wake_on(message)
    await _wake_on(message)
    assert len(effects) == 1
    assert len(sent) == 1
    assert sent[0].id.startswith("reply_")


@pytest.mark.asyncio
async def test_tampered_signed_message_is_refused_before_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = AgentIdentity.generate(org="local", agent_type="agent")
    agent = AgentIdentity.generate(org="local", agent_type="agent")
    message = _message(peer)
    message.body = "changed after signing"
    run_fn = AsyncMock()

    def prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
        kwargs["purpose"] = kwargs.pop("run_purpose")
        return CanonicalRunRequest(input_text=prompt, **kwargs)

    async def issuer(_request: CanonicalRunRequest, envelope: bytes) -> tuple[bytes, datetime]:
        if not verify_message(Message.model_validate_json(envelope), peer.public_key):
            raise ValueError("signed envelope changed")
        raise AssertionError("tamper accepted")

    state = SimpleNamespace(
        identity=agent,
        config=SimpleNamespace(entity_id="agent://me"),
        processing_lock=asyncio.Lock(),
        channel_last_woken={},
        requires_signed_runs=True,
        trigger_issuer=issuer,
        prepare_collected_request=prepare,
        agent_run_fn=run_fn,
        accepted_reply_fn=AsyncMock(),
        telemetry=None,
    )
    monkeypatch.setattr(_runtime, "state", lambda: state)
    with pytest.raises(ValueError, match="signed envelope changed"):
        await _wake_on(message)
    run_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_accepted_run_requests_unacked_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = AgentIdentity.generate(org="local", agent_type="agent")
    agent = AgentIdentity.generate(org="local", agent_type="agent")
    message = _message(peer)

    def prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
        kwargs["purpose"] = kwargs.pop("run_purpose")
        return CanonicalRunRequest(input_text=prompt, **kwargs)

    async def issuer(request: CanonicalRunRequest, envelope: bytes) -> tuple[bytes, datetime]:
        assert verify_message(Message.model_validate_json(envelope), peer.public_key)
        return request.digest().encode(), datetime.now(UTC) + timedelta(minutes=1)

    run_fn = AsyncMock(side_effect=RunAdmissionUnavailableError("anchor unavailable"))
    state = SimpleNamespace(
        identity=agent,
        config=SimpleNamespace(entity_id="agent://me"),
        processing_lock=asyncio.Lock(),
        channel_last_woken={},
        requires_signed_runs=True,
        trigger_issuer=issuer,
        prepare_collected_request=prepare,
        agent_run_fn=run_fn,
        accepted_reply_fn=AsyncMock(),
        telemetry=None,
    )
    monkeypatch.setattr(_runtime, "state", lambda: state)
    with pytest.raises(DeliveryUnavailableError):
        await _wake_on(message)
    run_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_verified_inbox_to_real_owner_reply_and_ack_once(tmp_path: Path) -> None:
    """A bus redelivery reaches the anchored result and never republishes its reply."""
    peer = AgentIdentity.generate(org="local", agent_type="agent")
    agent = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config=make_config_dict(entity_id="agent://me", entity_name="Me"),
        workspace=tmp_path,
        identity=agent,
        operator_signer=make_operator_signer(),
    )
    st = _runtime.state()
    await st.svc._audit.initialize()
    for identity, handle in ((agent, "me"), (peer, "peer")):
        await st.registry.register(
            Entity(
                did=identity.did,
                handle=handle,
                id=f"agent://{handle}",
                name=handle,
                type=EntityType.AGENT,
                public_key=identity.public_key.hex(),
            )
        )
    await st.svc.create_channel(Channel(name="team", members=["agent://me", "agent://peer"]))
    peer_svc = MessagingService(
        st.svc._backend,
        st.registry,
        st.svc._audit,
        signer=MessageSigner.from_identity(peer),
    )
    original = await peer_svc.send(
        Message(
            id="signed-actual-1", sender="agent://peer", to=["channel://team"], body="question"
        )
    )
    holder: dict[str, Any] = {}
    effects = 0
    reply_outage = True

    def prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
        kwargs["purpose"] = kwargs.pop("run_purpose")
        request = CanonicalRunRequest(input_text=prompt, **kwargs)
        holder["request"] = request
        return request

    async def issuer(request: CanonicalRunRequest, envelope: bytes) -> tuple[bytes, datetime]:
        asserted = Message.model_validate_json(envelope)
        assert verify_message(asserted, peer.public_key)
        assert asserted.id == request.occurrence_id
        assert asserted.signer_did == request.caller_did
        assert asserted.body in request.input_text
        return b"proof", datetime(2030, 1, 1, tzinfo=UTC)

    async def run_fn(_prompt: str, **kwargs: Any) -> arcrun.RunResult:
        nonlocal effects
        request = holder["request"]
        owner = holder.setdefault("owner", LedgerRunOwner(_ledger(request)))

        async def effect(_accepted: CanonicalRunRequest) -> arcrun.RunResult:
            nonlocal effects
            effects += 1
            return arcrun.RunResult(
                content="answer",
                turns=1,
                tool_calls_made=0,
                cost_usd=0,
                tokens_used={"input": 1, "output": 1, "total": 2},
            )

        return await owner.execute(
            request,
            signed_authorization=kwargs["signed_authorization"],
            deadline=kwargs["authorization_deadline"],
            max_result_bytes=500,
            invoke=effect,
        )

    async def reply_fn(run_id: str, *, send: Any, lookup: Any) -> str:
        nonlocal reply_outage
        owner = holder["owner"]
        if reply_outage:
            reply_outage = False
            anchor = owner._ledger._anchor
            original_latest = anchor.latest

            def unavailable() -> Any:
                raise TimeoutError("anchor unavailable after completed run")

            anchor.latest = unavailable
            try:
                return await owner.deliver_reply(run_id, send=send, lookup=lookup)
            finally:
                anchor.latest = original_latest
        return await owner.deliver_reply(run_id, send=send, lookup=lookup)

    st.requires_signed_runs = True
    st.trigger_issuer = issuer
    st.prepare_collected_request = prepare
    st.agent_run_fn = run_fn
    st.accepted_reply_fn = reply_fn
    delivery = SimpleNamespace(data=original.model_dump(), ack=AsyncMock())
    await st.svc._dispatch(delivery, _wake_on, set())
    delivery.ack.assert_not_awaited()
    assert effects == 1
    assert await st.svc.list_channel_messages("team", after_seq=original.seq) == []
    await st.svc._dispatch(delivery, _wake_on, set())
    await st.svc._dispatch(delivery, _wake_on, set())
    assert delivery.ack.await_count == 2
    assert effects == 1
    replies = await st.svc.list_channel_messages("team", after_seq=original.seq)
    assert [message.body for message in replies] == ["answer"]
    st.trigger_issuer = None
    unavailable = SimpleNamespace(data=original.model_dump(), ack=AsyncMock())
    await st.svc._dispatch(unavailable, _wake_on, set())
    unavailable.ack.assert_not_awaited()
    st.trigger_issuer = issuer
    st.reply_port = None
    await st.svc._dispatch(unavailable, _wake_on, set())
    unavailable.ack.assert_not_awaited()
