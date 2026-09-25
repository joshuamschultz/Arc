"""Anchored channel replies never repeat an uncertain bus side effect."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import arcrun
import arcstore
import arctrust
import pytest
from arcstore.backends.memory import FakeBackend

from arcagent.core.run_contract import CanonicalRunRequest
from arcagent.modules.run_intents.ledger import (
    ReplyDispatch,
    RunIntentLedger,
    VerifiedRunAuthorization,
)
from arcagent.modules.run_intents.owner import LedgerRunOwner

_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)


class Anchor:
    scope = RunIntentLedger.anchor_scope("tenant", "agent")

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


def _ledger(request: CanonicalRunRequest) -> RunIntentLedger:
    return RunIntentLedger(
        tenant_id="tenant", agent_did="agent",
        store=arcstore.AcceptedRunStore(
            FakeBackend(), arctrust.RecordCipher(bytes(range(32))),
            authorize=lambda _action, _tenant, _agent: True, audit_sink=Audit(),
        ),
        anchor=Anchor(),
        verify_authorization=lambda _evidence: VerifiedRunAuthorization(
            tenant_id="tenant", agent_did="agent", run_id=request.run_id,
            session_id=request.session_key, owner_epoch=1,
            request_digest=request.digest(), deadline=_DEADLINE,
            purpose=request.purpose, occurrence_id=request.occurrence_id, nonce="nonce-1",
        ),
        owner_epoch=1,
    )


async def _completed_reply_run() -> tuple[LedgerRunOwner, str]:
    request = CanonicalRunRequest(
        run_id="reply-run-1",
        session_key="channel-session",
        input_text="question",
        reply_target="channel://ops",
        caller_did="did:arc:user:alice",
        purpose="message",
        occurrence_id="message-1",
    )
    owner = LedgerRunOwner(_ledger(request))

    async def invoke(_accepted: CanonicalRunRequest) -> arcrun.RunResult:
        return arcrun.RunResult(
            content="answer", turns=1, tool_calls_made=0, cost_usd=0,
            tokens_used={"input": 1, "output": 1, "total": 2},
        )

    await owner.execute(
        request, signed_authorization=b"proof", deadline=_DEADLINE,
        max_result_bytes=500, invoke=invoke,
    )
    return owner, request.run_id


async def test_lost_send_response_reconciles_without_duplicate_reply() -> None:
    owner, run_id = await _completed_reply_run()
    sent: dict[str, object] = {}
    calls = 0

    async def send(reply: ReplyDispatch) -> None:
        nonlocal calls
        calls += 1
        sent[reply.message_id] = reply
        raise TimeoutError("publish response lost")

    async def lookup(reply: ReplyDispatch) -> bool:
        return sent.get(reply.message_id) == reply

    first = await owner.deliver_reply(run_id, send=send, lookup=lookup)
    repeated = await owner.deliver_reply(run_id, send=send, lookup=lookup)
    assert first == repeated == "sent"
    assert calls == 1


async def test_unprovable_reply_remains_unknown_until_external_reconciliation() -> None:
    owner, run_id = await _completed_reply_run()
    calls = 0
    visible = False

    async def send(_reply: ReplyDispatch) -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("transport ambiguous")

    async def lookup(_reply: ReplyDispatch) -> bool:
        return visible

    assert await owner.deliver_reply(run_id, send=send, lookup=lookup) == "outcome_unknown"
    assert await owner.deliver_reply(run_id, send=send, lookup=lookup) == "outcome_unknown"
    assert calls == 1
    visible = True
    assert await owner.deliver_reply(run_id, send=send, lookup=lookup) == "sent"
    assert calls == 1


async def test_crash_after_sending_anchor_never_republishes() -> None:
    owner, run_id = await _completed_reply_run()
    calls = 0

    async def send(_reply: ReplyDispatch) -> None:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    async def absent(_reply: ReplyDispatch) -> bool:
        return False

    with pytest.raises(asyncio.CancelledError):
        await owner.deliver_reply(run_id, send=send, lookup=absent)
    assert (await owner._ledger.get(run_id)).reply_state == "sending"
    owner._ledger._verify_authorization = lambda _evidence: (_ for _ in ()).throw(
        PermissionError("sender revoked")
    )
    recovered = await owner._ledger.recover()
    assert recovered[0].reply_state == "outcome_unknown"
    with pytest.raises(Exception, match="authorization refused"):
        await owner.deliver_reply(run_id, send=send, lookup=absent)
    assert calls == 1
