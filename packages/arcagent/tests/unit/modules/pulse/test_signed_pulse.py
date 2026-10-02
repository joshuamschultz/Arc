"""Pulse dispatch refuses unsigned edits and retains uncertain due slots."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from arcagent.core.control_contract import ControlArtifactRefusedError, SignedControlRevision
from arcagent.core.run_contract import CanonicalRunRequest, RunOutcomeUnknownError
from arcagent.modules.pulse import PulseCheck, PulseState
from arcagent.modules.pulse.config import PulseConfig
from arcagent.modules.pulse.engine import PulseEngine, parse_pulse_file
from arcagent.modules.pulse.signed_dispatch import (
    canonical_definition,
    dispatch_signed_pulse,
    pulse_occurrence,
)


def approved(check: PulseCheck) -> PulseCheck:
    return check.model_copy(
        update={
            "approval": SignedControlRevision(
                tenant_id="tenant",
                agent_did="did:arc:local:agent/one",
                purpose="pulse",
                artifact_id=check.name,
                revision=1,
                definition_digest=hashlib.sha256(canonical_definition(check)).hexdigest(),
                actor_did="did:arc:local:user/operator",
                issued_at=datetime.now(UTC),
                signature="ab" * 64,
            )
        }
    )


def prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
    kwargs["purpose"] = kwargs.pop("run_purpose")
    return CanonicalRunRequest(input_text=prompt, **kwargs)


async def issue(request: CanonicalRunRequest, evidence: bytes) -> tuple[bytes, datetime]:
    assert json.loads(evidence)["occurrence_id"] == request.occurrence_id
    return b"signed", datetime.now(UTC) + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_changed_pulse_action_is_refused_before_effect() -> None:
    check = approved(PulseCheck(name="health", interval_minutes=5, action="Check health"))
    changed = check.model_copy(update={"action": "Send secrets"})
    authority = AsyncMock()
    effect = AsyncMock()
    with pytest.raises(ControlArtifactRefusedError):
        await dispatch_signed_pulse(
            changed,
            prompt="task",
            due_at=datetime.now(UTC),
            tenant_id="tenant",
            agent_did="did:arc:local:agent/one",
            authority=authority,
            issuer=issue,
            prepare=prepare,
            run_fn=effect,
        )
    authority.verify_current.assert_not_awaited()
    effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_pulse_retains_same_run_after_restart(tmp_path: Path) -> None:
    check = approved(PulseCheck(name="health", interval_minutes=5, action="Check health"))
    seen: list[str] = []

    async def unknown(_prompt: str, **kwargs: Any) -> str:
        seen.append(kwargs["run_id"])
        raise RunOutcomeUnknownError("effect may have occurred")

    config = PulseConfig(timeout_seconds=5)
    first = PulseEngine(
        tmp_path,
        config,
        unknown,
        control_artifact_authority=AsyncMock(),
        control_tenant_id="tenant",
        agent_did="did:arc:local:agent/one",
        trigger_issuer=issue,
        prepare_collected_request=prepare,
    )
    await first._execute_check(check, PulseState())
    pending = first._read_state().checks[check.name]
    assert pending.last_run is None and pending.pending_due_at is not None
    due_at = datetime.fromisoformat(pending.pending_due_at)
    assert seen == [pulse_occurrence(check, due_at)[0]]

    second = PulseEngine(
        tmp_path,
        config,
        unknown,
        control_artifact_authority=AsyncMock(),
        control_tenant_id="tenant",
        agent_did="did:arc:local:agent/one",
        trigger_issuer=issue,
        prepare_collected_request=prepare,
    )
    await second._execute_check(check, second._read_state())
    assert seen == [seen[0], seen[0]]
    assert second._read_state().checks[check.name].pending_due_at == pending.pending_due_at


def test_invalid_approval_does_not_hide_other_pulse_checks() -> None:
    checks = parse_pulse_file(
        "## broken\n- **Interval:** 5 min\n- **Action:** Check one\n"
        "- **Approval:** {invalid}\n"
        "## healthy\n- **Interval:** 5 min\n- **Action:** Check two\n"
    )
    assert [check.name for check in checks] == ["healthy"]
