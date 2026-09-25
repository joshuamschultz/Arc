"""Ledger owner validates a canonical request and stores the real terminal result."""

from datetime import UTC, datetime

import arcrun
import arcstore
import arctrust
import pytest
from arcstore.backends.memory import FakeBackend

from arcagent.core.run_contract import CanonicalRunRequest, RunAdmissionRefusedError
from arcagent.modules.run_intents.ledger import (
    RunIntentLedger,
    VerifiedRunAuthorization,
)
from arcagent.modules.run_intents.owner import LedgerRunOwner

_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)


class Anchor:
    def __init__(self) -> None:
        self.scope = RunIntentLedger.anchor_scope("tenant", "agent")
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
    def write_durable(self, event: arctrust.AuditEvent) -> None:
        del event


def _ledger(request: CanonicalRunRequest) -> RunIntentLedger:
    return RunIntentLedger(
        tenant_id="tenant",
        agent_did="agent",
        store=arcstore.AcceptedRunStore(
            FakeBackend(),
            arctrust.RecordCipher(bytes(range(32))),
            authorize=lambda action, tenant, agent: True,
            audit_sink=Audit(),
        ),
        anchor=Anchor(),
        verify_authorization=lambda evidence: VerifiedRunAuthorization(
            tenant_id="tenant",
            agent_did="agent",
            run_id=request.run_id,
            session_id=request.session_key,
            owner_epoch=1,
            request_digest=request.digest(),
            deadline=_DEADLINE,
            purpose=request.purpose,
            occurrence_id=request.occurrence_id,
            nonce="nonce-1",
        ),
        owner_epoch=1,
    )


async def test_collected_result_roundtrips_real_run_fields() -> None:
    request = CanonicalRunRequest(
        run_id="run-1",
        session_key="session-1",
        input_text="request body",
        parts=[{"kind": "image", "ref": "objects/a", "sha256": "sha256:" + "a" * 64}],
        tool_choice={"type": "required"},
        max_tokens=50,
        reply_target="channel://ops",
        purpose="manual",
        occurrence_id="run-1",
    )
    ledger = _ledger(request)
    actual = arcrun.RunResult(
        content="done",
        turns=3,
        tool_calls_made=2,
        cost_usd=0.05,
        tokens_used={"input": 10, "output": 5, "total": 15},
        completion_payload={"status": "ok", "items": [{"id": "a"}]},
        completion_tool="signals_completion",
    )

    async def collect(accepted: CanonicalRunRequest) -> arcrun.RunResult:
        assert accepted == request
        return actual

    owner = LedgerRunOwner(ledger)
    result = await owner.execute(
        request,
        deadline=_DEADLINE,
        signed_authorization=b"signed:request",
        max_result_bytes=500,
        invoke=collect,
    )
    assert result == actual
    assert (
        await owner.execute(
            request,
            deadline=_DEADLINE,
            signed_authorization=b"signed:request",
            max_result_bytes=500,
            invoke=collect,
        )
        == actual
    )
    with pytest.raises(RunAdmissionRefusedError, match="admission refused"):
        await owner.execute(
            request.model_copy(update={"tool_choice": {"type": "none"}}),
            deadline=_DEADLINE,
            signed_authorization=b"signed:request",
            max_result_bytes=500,
            invoke=collect,
        )
    with pytest.raises(RunAdmissionRefusedError, match="admission refused"):
        await owner.execute(
            request.model_copy(
                update={"parts": [{"kind": "image", "ref": "objects/b", "sha256": "sha256:" + "a" * 64}]}
            ),
            deadline=_DEADLINE,
            signed_authorization=b"signed:request",
            max_result_bytes=500,
            invoke=collect,
        )


async def test_tool_uncertainty_is_anchored_and_redelivery_never_reexecutes() -> None:
    request = CanonicalRunRequest(
        run_id="run-unknown",
        session_key="session-1",
        input_text="perform external effect",
        purpose="manual",
        occurrence_id="request-1",
    )
    ledger = _ledger(request)
    unknown = arcrun.ToolOutcomeUnknown(
        run_id=request.run_id,
        tool_call_id="call-1",
        tool_name="send",
        invocation_key="invocation-1",
        turn_number=1,
        phase="completion_unconfirmed",
    )
    actual = arcrun.RunResult(
        content="",
        turns=1,
        tool_calls_made=1,
        cost_usd=0.01,
        tokens_used={"input": 1, "output": 1, "total": 2},
        outcome_unknown=unknown,
    )
    calls = 0

    async def collect(_accepted: CanonicalRunRequest) -> arcrun.RunResult:
        nonlocal calls
        calls += 1
        return actual

    owner = LedgerRunOwner(ledger)
    for _ in range(2):
        result = await owner.execute(
            request,
            deadline=_DEADLINE,
            signed_authorization=b"signed:request",
            max_result_bytes=500,
            invoke=collect,
        )
        assert result.outcome_unknown == unknown
    assert calls == 1
    assert (await ledger.get(request.run_id)).status == "outcome_unknown"
