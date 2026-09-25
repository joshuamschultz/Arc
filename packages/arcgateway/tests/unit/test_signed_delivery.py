"""Signed browser delivery binds one occurrence to one canonical agent request."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import arcagent
import arcrun
import arcstore
import arctrust
import pytest
from arcstore.backends.memory import FakeBackend

from arcgateway.executor import AsyncioExecutor, InboundEvent


class SignedAgent:
    requires_signed_runs = True

    def __init__(self) -> None:
        self.prepared: list[arcagent.CanonicalRunRequest] = []
        self.delivered: list[dict[str, Any]] = []

    def prepare_delivered_request(self, **kwargs: Any) -> arcagent.CanonicalRunRequest:
        request = arcagent.CanonicalRunRequest(
            run_id=kwargs["run_id"],
            session_key=kwargs["session_key"],
            input_text=kwargs["message"],
            parts=kwargs.get("parts"),
            reply_target=kwargs["reply_target"],
            reply_label=kwargs["reply_label"],
            caller_did=kwargs["caller_did"],
            purpose="message",
            occurrence_id=kwargs["occurrence_id"],
        )
        self.prepared.append(request)
        return request

    async def stream_delivered_message(self, **kwargs: Any) -> Any:
        self.delivered.append(kwargs)
        yield arcagent.DeliveryTextEvent(run_id=kwargs["run_id"], sequence=1, text="hello")
        yield arcagent.DeliveryTerminalEvent(
            run_id=kwargs["run_id"], sequence=2, status="completed"
        )


def event(
    *, session: str = "session-1", occurrence: str | None = "2ac7c119-7a7e-4ad9-a735-65dcf8c8b507"
) -> InboundEvent:
    return InboundEvent(
        platform="web",
        chat_id="chat-1",
        user_did="did:arc:user:alice",
        agent_did="did:arc:agent:bot",
        session_key=session,
        message="hello",
        occurrence_id=occurrence,
    )


@pytest.mark.asyncio
async def test_signed_delivery_uses_exact_prepared_request_and_live_stream() -> None:
    agent = SignedAgent()
    issued: list[arcagent.CanonicalRunRequest] = []
    deadline = datetime(2030, 1, 1, tzinfo=UTC)

    async def issue(inbound: InboundEvent, request: arcagent.CanonicalRunRequest) -> Any:
        assert inbound.user_did == request.caller_did
        issued.append(request)
        return (b"signed-proof", deadline)

    async def factory(_did: str) -> SignedAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=factory, run_authorization_issuer=issue)
    first = [delta async for delta in await executor.run(event())]
    second = [delta async for delta in await executor.run(event())]
    assert [delta.kind for delta in first] == ["token", "done"]
    assert first[-1].status == "completed"
    assert issued == agent.prepared
    assert issued[0].run_id == issued[1].run_id
    assert agent.delivered[0]["signed_authorization"] == b"signed-proof"
    assert agent.delivered[0]["authorization_deadline"] == deadline
    assert agent.delivered[0]["run_id"] == issued[0].run_id
    assert second[-1].turn_id == first[-1].turn_id


@pytest.mark.asyncio
async def test_signed_delivery_refuses_missing_occurrence_or_issuer() -> None:
    agent = SignedAgent()

    async def factory(_did: str) -> SignedAgent:
        return agent

    executor = AsyncioExecutor(agent_factory=factory)
    missing_issuer = [delta async for delta in await executor.run(event())]
    missing_occurrence = [delta async for delta in await executor.run(event(occurrence=None))]
    assert missing_issuer[-1].status == "failed"
    assert missing_occurrence[-1].status == "failed"
    assert not agent.delivered


@pytest.mark.asyncio
async def test_same_occurrence_in_other_session_has_other_run_identity() -> None:
    agent = SignedAgent()

    async def factory(_did: str) -> SignedAgent:
        return agent

    async def issue(_inbound: InboundEvent, _request: arcagent.CanonicalRunRequest) -> Any:
        return (b"proof", datetime(2030, 1, 1, tzinfo=UTC))

    executor = AsyncioExecutor(agent_factory=factory, run_authorization_issuer=issue)
    _ = [delta async for delta in await executor.run(event())]
    _ = [delta async for delta in await executor.run(event(session="session-2"))]
    assert agent.prepared[0].run_id != agent.prepared[1].run_id


@pytest.mark.asyncio
async def test_gateway_redelivery_uses_ledger_once_and_refuses_swapped_body() -> None:
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
    issued: dict[bytes, arcagent.VerifiedRunAuthorization] = {}

    class Anchor:
        scope = arcagent.RunIntentLedger.anchor_scope("tenant", "did:arc:agent:bot")

        def __init__(self) -> None:
            self.head: arctrust.AnchorHead | None = None

        def latest(self) -> arctrust.AnchorHead | None:
            return self.head

        def compare_and_advance(
            self, expected: arctrust.AnchorHead | None, digest: str, intent: str
        ) -> arctrust.AnchorHead:
            if self.head != expected:
                raise RuntimeError("stale anchor")
            self.head = arctrust.AnchorHead(
                scope=self.scope,
                version=1 if expected is None else expected.version + 1,
                digest=digest,
                previous_digest=None if expected is None else expected.digest,
                intent=intent,
            )
            return self.head

    class Audit:
        def write_durable(self, _event: arctrust.AuditEvent) -> None:
            return

    ledger = arcagent.RunIntentLedger(
        tenant_id="tenant",
        agent_did="did:arc:agent:bot",
        store=arcstore.AcceptedRunStore(
            FakeBackend(),
            arctrust.RecordCipher(bytes(range(32))),
            authorize=lambda _action, _tenant, _agent: True,
            audit_sink=Audit(),
        ),
        anchor=Anchor(),
        verify_authorization=lambda evidence: issued[evidence],
        owner_epoch=1,
    )
    owner = arcagent.LedgerRunOwner(ledger)

    class LedgerAgent(SignedAgent):
        effects = 0

        async def stream_delivered_message(self, **kwargs: Any) -> Any:
            self.delivered.append(kwargs)
            request = self.prepare_delivered_request(
                caller_did=kwargs["caller_did"],
                message=kwargs["message"],
                session_key=kwargs["session_key"],
                reply_target=kwargs["reply_target"],
                reply_label=kwargs["reply_label"],
                run_id=kwargs["run_id"],
                occurrence_id=kwargs["occurrence_id"],
            )

            async def invoke(_accepted: arcagent.CanonicalRunRequest) -> arcrun.RunResult:
                self.effects += 1
                return arcrun.RunResult(
                    content="reply",
                    turns=1,
                    tool_calls_made=0,
                    cost_usd=0,
                    tokens_used={"input": 1, "output": 1, "total": 2},
                )

            result = await owner.execute(
                request,
                signed_authorization=kwargs["signed_authorization"],
                deadline=kwargs["authorization_deadline"],
                max_result_bytes=500,
                invoke=invoke,
            )
            yield arcagent.DeliveryTextEvent(
                run_id=request.run_id, sequence=1, text=result.content
            )
            yield arcagent.DeliveryTerminalEvent(
                run_id=request.run_id, sequence=2, status="completed"
            )

    agent = LedgerAgent()

    async def factory(_did: str) -> LedgerAgent:
        return agent

    async def issue(
        _inbound: InboundEvent, request: arcagent.CanonicalRunRequest
    ) -> tuple[bytes, datetime]:
        evidence = request.digest().encode()
        issued[evidence] = arcagent.VerifiedRunAuthorization(
            tenant_id="tenant",
            agent_did="did:arc:agent:bot",
            run_id=request.run_id,
            session_id=request.session_key,
            owner_epoch=1,
            request_digest=request.digest(),
            deadline=deadline,
            purpose="message",
            occurrence_id=request.occurrence_id,
            nonce="nonce-1",
        )
        return evidence, deadline

    executor = AsyncioExecutor(agent_factory=factory, run_authorization_issuer=issue)
    first = [delta async for delta in await executor.run(event())]
    repeated = [delta async for delta in await executor.run(event())]
    forged = [
        delta
        async for delta in await executor.run(event().model_copy(update={"message": "swapped"}))
    ]
    assert first[-1].status == repeated[-1].status == "completed"
    assert first[-1].turn_id == repeated[-1].turn_id
    assert forged[-1].status == "failed"
    assert agent.effects == 1
