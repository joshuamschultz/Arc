"""Optional ledger-backed implementation of ArcAgent's accepted-run contract."""

from __future__ import annotations

from datetime import datetime

import arcrun
from pydantic import TypeAdapter

from arcagent.core.run_contract import (
    CanonicalRunRequest,
    RunAdmissionRefusedError,
    RunInvoker,
    RunOutcomeUnknownError,
)
from arcagent.modules.run_intents.ledger import (
    ReplyLookup,
    ReplySender,
    RunIntentLedger,
    RunIntentUnavailableError,
)
from arcagent.modules.run_intents.ledger import (
    RunOutcomeUnknownError as EffectOutcomeUnknownError,
)

_RESULT_ADAPTER = TypeAdapter(arcrun.RunResult)


class LedgerRunOwner:
    """Persist signed canonical work before invoking the agent loop."""

    def __init__(self, ledger: RunIntentLedger) -> None:
        self._ledger = ledger

    async def deliver_reply(
        self, run_id: str, *, send: ReplySender, lookup: ReplyLookup
    ) -> str:
        """Deliver or reconcile one anchored channel reply without re-sending unknown work."""
        return await self._ledger.deliver_reply(run_id, send=send, lookup=lookup)

    async def execute(
        self,
        request: CanonicalRunRequest,
        *,
        signed_authorization: bytes,
        deadline: datetime,
        max_result_bytes: int,
        invoke: RunInvoker,
    ) -> arcrun.RunResult:
        """Return a stored result on redelivery without replaying the effect."""
        ledger = self._ledger
        try:
            intent = await ledger.accept(
                run_id=request.run_id,
                session_id=request.session_key,
                owner_epoch=ledger.owner_epoch,
                deadline=deadline,
                request=request.canonical_bytes(),
                signed_authorization=signed_authorization,
                max_result_bytes=max_result_bytes,
                purpose=request.purpose,
                occurrence_id=request.occurrence_id,
            )
        except RunIntentUnavailableError as exc:
            raise RunAdmissionRefusedError("signed run admission refused") from exc

        async def effect(body: bytes) -> bytes:
            try:
                stored = CanonicalRunRequest.model_validate_json(body)
            except ValueError as exc:
                raise RunIntentUnavailableError("accepted run request is invalid") from exc
            if stored != request or stored.canonical_bytes() != body:
                raise RunIntentUnavailableError("accepted run request changed")
            result = await invoke(stored)
            payload = _RESULT_ADAPTER.dump_json(result)
            if result.outcome_unknown is not None:
                raise EffectOutcomeUnknownError(payload)
            return payload

        if intent.status == "accepted":
            try:
                intent = await ledger.execute(intent, effect)
            except Exception as exc:
                raise RunOutcomeUnknownError("accepted run outcome unavailable") from exc
        if intent.status not in ("completed", "outcome_unknown") or intent.result_ref is None:
            raise RunOutcomeUnknownError(f"run {request.run_id} is {intent.status}")
        payload = await ledger.result_bytes(intent)
        try:
            return _RESULT_ADAPTER.validate_json(payload)
        except ValueError as exc:
            raise RunIntentUnavailableError("stored run result is invalid") from exc
